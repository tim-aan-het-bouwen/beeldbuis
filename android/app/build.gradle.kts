plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// The interface is the TV web app in ../tv/app, copied into the APK's assets at build time.
val copyWebApp by tasks.registering(Copy::class) {
    from(rootProject.file("../tv/app")) { exclude("config.xml", "icon.png", "js/hls.min.js") }
    into(layout.buildDirectory.dir("generated/webassets/web"))
}

android {
    namespace = "app.beeldbuis.tv"
    compileSdk = 35

    defaultConfig {
        applicationId = "app.beeldbuis.tv"
        minSdk = 21
        targetSdk = 35
        versionCode = (System.getenv("BB_VERSION_CODE") ?: "1").toInt()
        versionName = System.getenv("BB_VERSION_NAME") ?: "0.1.0"
    }

    // A fixed key so a new version installs over the old one. CI passes it in; without it the build
    // falls back to the debug key (fine for trying it, but updates then need an uninstall first).
    val ks = System.getenv("BB_KEYSTORE")
    signingConfigs {
        if (ks != null) create("release") {
            storeFile = file(ks)
            storePassword = System.getenv("BB_KEYSTORE_PASSWORD")
            keyAlias = System.getenv("BB_KEY_ALIAS") ?: "beeldbuis"
            keyPassword = System.getenv("BB_KEY_PASSWORD") ?: System.getenv("BB_KEYSTORE_PASSWORD")
        }
    }
    buildTypes {
        getByName("release") {
            isMinifyEnabled = false
            signingConfig = if (ks != null) signingConfigs.getByName("release") else signingConfigs.getByName("debug")
        }
    }
    sourceSets["main"].assets.srcDir(layout.buildDirectory.dir("generated/webassets"))
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
}

tasks.named("preBuild") { dependsOn(copyWebApp) }

dependencies {
    val media3 = "1.5.1"
    implementation("androidx.media3:media3-exoplayer:$media3")
    implementation("androidx.media3:media3-exoplayer-hls:$media3")
    implementation("androidx.media3:media3-ui:$media3")
    implementation("androidx.webkit:webkit:1.12.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
}
