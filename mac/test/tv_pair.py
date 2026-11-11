#!/usr/bin/env python3
"""Pairing with a Samsung TV that asks for permission.

A made-up TV on 127.0.0.2: the open door (:8001) and the secured door (:8002, TLS with its own certificate). Each
scenario sets how the TV behaves and checks what tv_ws() in the built iptv.py does: which door, which name, whether
the token is kept and used again, and which message the user gets.

    python3 test/tv_pair.py
"""
import base64, hashlib, importlib.util, json, os, socket, ssl, struct, subprocess, sys, tempfile, threading, time, urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TMP = tempfile.mkdtemp(prefix="bbpair")
os.environ["IPTV_CONFIG_DIR"] = os.path.join(TMP, "cfg")

TV = {"open": "accept",        # the open door: accept | unauthorized | closed
      "secure": "ask",         # the secured door: ask | closed
      "denied": set(),         # names the TV refuses at once
      "allowed": {},           # name -> token
      "answer": "allow",       # what the viewer does with the question: allow | ignore
      "delay": 1.0,            # seconds before the viewer answers
      "ignore_after": 2.0,     # the TV gives up on the question after this long
      "log": []}               # (port, name, token, result) and the keys that arrived


def frame(text):
    d = text.encode()
    return bytes([0x81, len(d)]) + d if len(d) < 126 else bytes([0x81, 126]) + struct.pack(">H", len(d)) + d


def client(c, port):
    try:
        head = b""
        while b"\r\n\r\n" not in head:
            part = c.recv(4096)
            if not part:
                return
            head += part
        line = head.split(b"\r\n", 1)[0].decode()
        q = urllib.parse.parse_qs(urllib.parse.urlparse(line.split(" ")[1]).query)
        name = base64.b64decode(q.get("name", [""])[0]).decode()
        token = q.get("token", [""])[0]
        key = [l.split(b":", 1)[1].strip() for l in head.split(b"\r\n") if l.lower().startswith(b"sec-websocket-key")][0]
        acc = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
        c.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: " + acc + b"\r\n\r\n")

        def say(event, data=None, result=None):
            TV["log"].append((port, name, token, result or event))
            c.sendall(frame(json.dumps({"event": event, "data": data or {}})))

        if port == 8001:
            if TV["open"] == "unauthorized" or name in TV["denied"]:
                return say("ms.channel.unauthorized")
            say("ms.channel.connect")
        else:
            if name in TV["denied"]:
                return say("ms.channel.unauthorized")
            if token and TV["allowed"].get(name) == token:
                say("ms.channel.connect", {"token": token})
            elif token:
                return say("ms.channel.unauthorized")
            else:
                TV["log"].append((port, name, "", "question"))
                if TV["answer"] == "ignore":
                    time.sleep(TV["ignore_after"])
                    return say("ms.channel.timeOut")
                time.sleep(TV["delay"])
                tk = str(10000000 + len(TV["allowed"]))
                TV["allowed"][name] = tk
                say("ms.channel.connect", {"token": tk})
        buf = b""
        while True:
            part = c.recv(4096)
            if not part:
                return
            buf += part
            while len(buf) >= 6:
                n = buf[1] & 0x7F; off = 2
                if n == 126:
                    n = struct.unpack(">H", buf[2:4])[0]; off = 4
                if len(buf) < off + 4 + n:
                    break
                mask = buf[off:off + 4]; data = bytes(b ^ mask[i % 4] for i, b in enumerate(buf[off + 4:off + 4 + n]))
                buf = buf[off + 4 + n:]
                TV["log"].append(("key", json.loads(data.decode())["params"]["DataOfCmd"]))
    except (OSError, ValueError):
        pass
    finally:
        try:
            c.close()
        except OSError:
            pass


def door(port, ctx=None):
    srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.2", port)); srv.listen(8)

    def one(c):
        mode = TV["open"] if port == 8001 else TV["secure"]
        if mode == "closed":
            c.close(); return
        if ctx:
            try:
                c = ctx.wrap_socket(c, server_side=True)
            except (OSError, ssl.SSLError):
                return
        client(c, port)

    def loop():
        while True:
            c, _ = srv.accept()
            threading.Thread(target=one, args=(c,), daemon=True).start()
    threading.Thread(target=loop, daemon=True).start()


def main():
    crt, key = os.path.join(TMP, "tv.crt"), os.path.join(TMP, "tv.key")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=SmartViewSDK",
                    "-keyout", key, "-out", crt], check=True, capture_output=True)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain(crt, key)
    door(8001); door(8002, ctx)

    spec = importlib.util.spec_from_file_location("iptv", os.path.join(ROOT, "pkg", "iptv.py"))
    bb = importlib.util.module_from_spec(spec); spec.loader.exec_module(bb)
    S = bb.STATE["settings"]
    fails = []

    def check(what, ok, extra=""):
        print(("  ok    " if ok else "  FAIL  ") + what + ((" :: " + str(extra)) if extra and not ok else ""))
        if not ok:
            fails.append(what)

    def reset(**kw):
        TV.update(open="accept", secure="ask", denied=set(), allowed={}, answer="allow", delay=1.0, ignore_after=2.0, log=[])
        TV.update(kw)
        S.update(tv_ip="127.0.0.2", tv_token="", tv_name="")

    def press(k="KEY_VOLUP"):
        try:
            bb.tv_ws([bb.tv_key_msg(k)], gap=0.2)
            time.sleep(0.2)
            return ""
        except RuntimeError as e:
            return str(e)

    def keys():
        return [x[1] for x in TV["log"] if x[0] == "key"]

    print("1. older TV: the open door takes keys without asking")
    reset()
    check("key arrives", press() == "" and keys() == ["KEY_VOLUP"], TV["log"])
    check("no question, no token", not S.get("tv_token") and not any(x[0] == 8002 for x in TV["log"]))

    print("2. open door says unauthorized: question on the secured door, Allow, token kept")
    reset(open="unauthorized")
    t0 = time.time(); err = press()
    check("the press waits for Allow and then lands", err == "" and keys() == ["KEY_VOLUP"] and time.time() - t0 >= 1.0, (err, TV["log"]))
    check("token and name saved", S.get("tv_token") == "10000000" and S.get("tv_name") == "Beeldbuis", (S.get("tv_token"), S.get("tv_name")))
    saved = json.load(open(os.path.join(os.environ["IPTV_CONFIG_DIR"], "config.json")))["settings"]
    check("token written to config.json", saved.get("tv_token") == "10000000")
    n = len(TV["log"]); t0 = time.time(); err = press("KEY_MUTE")
    new = TV["log"][n:]
    check("next press: straight in with the token, no question", err == "" and keys()[-1] == "KEY_MUTE" and time.time() - t0 < 1.0
          and not any(x[-1] == "question" for x in new) and new[0][:3] == (8002, "Beeldbuis", "10000000"), new)

    print("3. the name was denied on the TV earlier (refused without a question): new name, question, Allow")
    reset(open="unauthorized", denied={"Beeldbuis"})
    err = press()
    nm = S.get("tv_name") or ""
    check("press lands under a new name", err == "" and keys() == ["KEY_VOLUP"] and nm.startswith("Beeldbuis ") and nm != "Beeldbuis", (err, nm, TV["log"]))
    check("token saved for the new name", S.get("tv_token") and TV["allowed"].get(nm) == S.get("tv_token"))
    n = len(TV["log"]); err = press("KEY_MUTE")
    check("next press uses the new name and its token", err == "" and TV["log"][n][:2] == (8002, nm) and not any(x[-1] == "question" for x in TV["log"][n:]), TV["log"][n:])

    print("4. nobody answers the question")
    reset(open="unauthorized", answer="ignore")
    err = press()
    check("clear message, nothing saved", "choose Allow on the TV screen" in err and not S.get("tv_token") and keys() == [], err)

    print("5. two presses while the question is on screen")
    reset(open="unauthorized", delay=1.5)
    out = []
    th = threading.Thread(target=lambda: out.append(press("KEY_UP"))); th.start()
    time.sleep(0.6)
    err2 = press("KEY_DOWN"); th.join()
    check("second press gets the Allow message at once, only one question on the TV",
          "choose Allow" in err2 and out == [""] and sum(1 for x in TV["log"] if x[-1] == "question") == 1, (err2, out, TV["log"]))

    print("6. the TV forgot the token (reset, or removed from the device list)")
    reset(open="unauthorized")
    S.update(tv_token="99", tv_name="Beeldbuis")
    err = press()
    check("pairs again and keeps the new token", err == "" and keys() == ["KEY_VOLUP"] and S.get("tv_token") == "10000000", (err, S.get("tv_token"), TV["log"]))

    print("7. no secured door, the name is denied on the open door")
    reset(secure="closed", denied={"Beeldbuis"})
    err = press()
    check("lands on the open door under a new name", err == "" and keys() == ["KEY_VOLUP"] and (S.get("tv_name") or "").startswith("Beeldbuis "), (err, TV["log"]))
    n = len(TV["log"]); err = press("KEY_MUTE")
    check("next press goes straight to the open door under that name", err == "" and TV["log"][n][:2] == (8001, S.get("tv_name")), TV["log"][n:])

    print("8. the TV refuses everything")
    reset(open="unauthorized", secure="closed")
    err = press()
    check("message says where to switch it on", "Device Connection Manager" in err, err)

    print("9. TV off")
    reset()
    S.update(tv_ip="127.0.0.3")
    err = press()
    check("message says the TV does not answer", "does not answer" in err, err)

    print("\n%d failed" % len(fails) if fails else "\nall passed")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
