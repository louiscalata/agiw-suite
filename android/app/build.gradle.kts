plugins {
    id("com.android.application")
}
android {
    namespace = "suite.agiw.monitor"
    compileSdk = 35
    buildToolsVersion = "35.0.0"
    defaultConfig {
        applicationId = "suite.agiw.monitor"
        minSdk = 26
        targetSdk = 35
        versionCode = 2
        versionName = "1.1.0"
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
        encoding = "UTF-8"
    }
}

tasks.withType<JavaCompile>().configureEach {
    options.encoding = "UTF-8"
}

dependencies {
    implementation(project(":monitor-core"))
}
