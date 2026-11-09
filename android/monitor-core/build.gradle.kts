plugins {
    java
}

java {
    sourceCompatibility = JavaVersion.VERSION_17
    targetCompatibility = JavaVersion.VERSION_17
}

// Windows javac defaults to a legacy code page. The acceptance line is UTF-8,
// and without this the middle dot is compiled as U+00C2 U+00B7 ("Â·").
tasks.withType<JavaCompile>().configureEach {
    options.encoding = "UTF-8"
}

dependencies {
    testImplementation("junit:junit:4.13.2")
}

tasks.test {
    useJUnit()
    filter {
        includeTestsMatching("suite.agiw.monitor.MonitorCoreGradleTest")
    }
    testLogging {
        events("passed", "failed", "skipped")
        showStandardStreams = true
    }
}
