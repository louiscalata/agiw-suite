plugins {
    id("com.android.application")
}
android {
    namespace = "suite.agiw.monitor"
    compileSdk = 35
    defaultConfig {
        applicationId = "suite.agiw.monitor"
        minSdk = 26
        targetSdk = 35
        versionCode = 1
        versionName = "0.1.0"
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}
dependencies {
    implementation(project(":monitor-core"))
}
