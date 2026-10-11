#include <WiFi.h>
#include <HTTPClient.h>
#include <Wire.h>
#include <MPU6050.h>
#include <math.h>

MPU6050 mpu;

// ---------------- WIFI (HOTSPOT) ----------------
const char* ssid     = "Fall detector";
const char* password = "@andha123";

// Backend (laptop hotspot IP)
String server = "http://192.168.137.1:8000/sensor";

// ---------------- AUTH ----------------
// Paste a fresh valid session token here before each demo/test session.
// Tokens expire after 24h (see SESSION_EXPIRE_HOURS in main.py) and the
// in-memory session store resets every time the backend restarts.
String SESSION_TOKEN = "5ecce2583643096ea4b9e50350d557daecc191512e997f184f8f613476b005a5";

// ---------------- TIMING ----------------
unsigned long lastSend = 0;
const long interval = 2000;   // how often we POST while in normal motion

// ---------------- MOTION THRESHOLDS ----------------
const float STILL_ACC_LOW  = 0.90;
const float STILL_ACC_HIGH = 1.10;
const float MOTION_GYRO_THRESHOLD = 20.0;
const float FALL_ACC_THRESHOLD = 2.5;
const float FALL_GYRO_THRESHOLD = 200.0;

// ---------------- FALL CONFIRMATION STATE ----------------
// A real fall = spike (high acc + high gyro) followed by stillness.
// We confirm in two stages so a brief shake/vibration alone can't trigger
// a false alert — only spike-THEN-stillness counts as a real fall.
enum FallState { MONITORING, IMPACT_DETECTED, FALL_CONFIRMED };
FallState fallState = MONITORING;

unsigned long impactTime = 0;
const unsigned long STILLNESS_REQUIRED      = 1500;    // must stay still this long after impact
const unsigned long FALL_WINDOW             = 3000;    // if no stillness within this long, was not a fall
const unsigned long FALL_CONFIRMED_TIMEOUT  = 60000UL; // safety: force reset after 60s if stuck

int impactStreak = 0;
const int IMPACT_STREAK_REQUIRED = 1;   // single spike sufficient — thresholds (2.5g / 200dps) are strict enough

bool alertSent = false;   // tracks whether we've already POSTed fall=1 for this event

// ---------------- SNAPSHOT OF IMPACT VALUES ----------------
// Saved at the moment the impact spike is confirmed so that when we POST
// fall=1 (which may happen milliseconds later at resting sensor values)
// the DB row always shows the real impact acc/gyro, not resting noise.
float fallAcc  = 0.0;
float fallGyro = 0.0;

// ---------------- DIAGNOSTIC PRINTING ----------------
// Prints every loop so you can watch the state machine live in Serial
// Monitor instead of guessing from 2-second-spaced database rows.
// Set to false once everything is confirmed working, to reduce clutter.
const bool VERBOSE = true;

// ---------------- WIFI RECONNECT FUNCTION ----------------
void connectWiFi() {
    WiFi.disconnect(true);
    delay(1000);

    WiFi.begin(ssid, password);
    Serial.print("Connecting WiFi");

    int retry = 0;
    while (WiFi.status() != WL_CONNECTED && retry < 20) {
        delay(500);
        Serial.print(".");
        retry++;
    }

    if (WiFi.status() == WL_CONNECTED) {
        Serial.println("\nWiFi Connected");
        Serial.println(WiFi.localIP());
    } else {
        Serial.println("\nWiFi Failed");
    }
}

// ---------------- SETUP ----------------
void setup() {
    Serial.begin(115200);
    delay(500);
    Serial.println("\n=== IoT Fall Detection (Email-only build) ===");
    Serial.println(server);

    connectWiFi();

    Wire.begin(21, 22);
    mpu.initialize();

    if (mpu.testConnection()) {
        Serial.println("MPU6050 Connected");
    } else {
        Serial.println("MPU6050 ERROR - check wiring");
    }

    Serial.println("Setup complete. Entering monitoring loop...\n");
}

// ---------------- LOOP ----------------
void loop() {
    int16_t ax, ay, az, gx, gy, gz;
    mpu.getMotion6(&ax, &ay, &az, &gx, &gy, &gz);

    float acc  = sqrt((float)ax * (float)ax + (float)ay * (float)ay + (float)az * (float)az) / 16384.0;
    float gyro = sqrt((float)gx * (float)gx + (float)gy * (float)gy + (float)gz * (float)gz) / 131.0;

    bool isStill     = (acc >= STILL_ACC_LOW && acc <= STILL_ACC_HIGH && gyro < MOTION_GYRO_THRESHOLD);
    bool motion      = !isStill;
    bool impactSpike = (acc > FALL_ACC_THRESHOLD && gyro > FALL_GYRO_THRESHOLD);

    int fallFlag = 0;

    // ---------------- FALL CONFIRMATION STATE MACHINE ----------------
    switch (fallState) {

        case MONITORING:
            if (impactSpike) {
                impactStreak++;
                if (impactStreak >= IMPACT_STREAK_REQUIRED) {
                    // FIX 2: snapshot the real impact values RIGHT HERE
                    // so the fall=1 POST always carries spike-level acc/gyro
                    // regardless of when the HTTP request actually fires.
                    fallAcc      = acc;
                    fallGyro     = gyro;
                    fallState    = IMPACT_DETECTED;
                    impactTime   = millis();
                    impactStreak = 0;
                    alertSent    = false;
                    Serial.print(">>> Impact spike confirmed (acc=");
                    Serial.print(acc, 2);
                    Serial.print(" gyro=");
                    Serial.print(gyro, 2);
                    Serial.println(") -> waiting for stillness...");
                }
            } else {
                impactStreak = 0;
            }
            break;

        case IMPACT_DETECTED:
            if (isStill) {
                if (millis() - impactTime >= STILLNESS_REQUIRED) {
                    fallState = FALL_CONFIRMED;
                    Serial.println(">>> FALL CONFIRMED -> stillness after impact detected!");
                }
            } else if (millis() - impactTime > FALL_WINDOW) {
                fallState  = MONITORING;
                fallAcc    = 0.0;
                fallGyro   = 0.0;
                Serial.println(">>> Spike was not a fall (kept moving). Resetting.");
            }
            break;

        case FALL_CONFIRMED:
            // FIX 1: only raise fallFlag if alert has NOT been sent yet.
            // Prevents repeated fall=1 rows in the DB while device lies still.
            if (!alertSent) {
                fallFlag = 1;
            }

            // Exit 1: person/device moved again
            if (!isStill && acc > 0.5) {
                fallState  = MONITORING;
                fallFlag   = 0;
                alertSent  = false;
                fallAcc    = 0.0;
                fallGyro   = 0.0;
                Serial.println(">>> Movement detected after fall. Resuming monitoring.");
            }
            // Exit 2: safety timeout, prevents permanent stuck state
            else if (millis() - impactTime > FALL_CONFIRMED_TIMEOUT) {
                fallState  = MONITORING;
                fallFlag   = 0;
                alertSent  = false;
                fallAcc    = 0.0;
                fallGyro   = 0.0;
                Serial.println(">>> Timeout reached with no movement. Resetting state.");
            }
            break;
    }

    if (VERBOSE) {
        Serial.print("[LOOP] acc="); Serial.print(acc, 2);
        Serial.print(" gyro="); Serial.print(gyro, 2);
        Serial.print(" still="); Serial.print(isStill ? "Y" : "N");
        Serial.print(" spike="); Serial.print(impactSpike ? "Y" : "N");
        Serial.print(" streak="); Serial.print(impactStreak);
        Serial.print(" state=");
        Serial.println(fallState == MONITORING ? "MONITORING" :
                        fallState == IMPACT_DETECTED ? "IMPACT_DETECTED" : "FALL_CONFIRMED");
    }

    // ---------------- SEND TO BACKEND ----------------
    // Send immediately and unconditionally the moment a fall is confirmed
    // (don't wait for the normal 2s motion-interval throttle).
    bool sendNow = false;

    if (fallFlag == 1 && !alertSent) {
        sendNow = true;
    } else if (motion && (millis() - lastSend >= interval)) {
        sendNow = true;
    }

    if (sendNow) {
        lastSend = millis();

        if (WiFi.status() == WL_CONNECTED) {

            HTTPClient http;
            http.setTimeout(3000);
            http.begin(server);
            http.addHeader("Content-Type", "application/json");
            http.addHeader("Authorization", "Bearer " + SESSION_TOKEN);

            // FIX 2: when posting fall=1, use the snapshotted impact values
            // (fallAcc / fallGyro) so the DB row always shows spike-level
            // readings — not whatever the sensor happens to read at POST time.
            String json = "{";
            if (fallFlag == 1) {
                json += "\"acc\":"  + String(fallAcc,  2);
                json += ",\"gyro\":" + String(fallGyro, 2);
            } else {
                json += "\"acc\":"  + String(acc,  2);
                json += ",\"gyro\":" + String(gyro, 2);
            }
            json += ",\"fall\":" + String(fallFlag);
            json += "}";

            int httpCode = http.POST(json);

            Serial.print("HTTP: ");
            Serial.println(httpCode);

            if (httpCode != 200) {
                String body = http.getString();
                Serial.println("Response body: " + body);
            }

            http.end();

            if (fallFlag == 1) {
                if (httpCode == 200) {
                    alertSent = true;
                    Serial.println(">>> Fall alert sent to backend. Email should be on its way.");
                } else {
                    Serial.println(">>> Fall alert POST failed - will retry next loop.");
                }
            }

            if (httpCode <= 0) {
                Serial.println("Backend unreachable -> retrying WiFi...");
                connectWiFi();
            }

        } else {
            Serial.println("WiFi lost -> reconnecting...");
            connectWiFi();
        }
    }

    delay(100);
}
