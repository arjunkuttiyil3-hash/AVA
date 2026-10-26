/*
 * Minimal AVA serial protocol example for Arduino/ESP32.
 * G:<GESTURE> and S:<text> are newline-terminated UTF-8 commands.
 * Replace gesture() with safe, hardware-specific motor/servo control.
 */

String lineBuffer;

void setup() {
  Serial.begin(115200);
  lineBuffer.reserve(192);
}

void gesture(const String& name) {
  if (name == "STOP") {
    // Add hardware-specific emergency stop here.
  } else if (name == "WAVE" || name == "POINT" ||
             name == "EXPRESSIVE" || name == "BOW") {
    // Map this named gesture to tested servo or motor actions.
  } else {
    Serial.println("ERR:UNKNOWN_GESTURE");
    return;
  }
  Serial.println("OK");
}

void handleCommand(const String& command) {
  if (command.startsWith("G:")) {
    gesture(command.substring(2));
  } else if (command.startsWith("S:")) {
    // Forward to a compatible audio/display peripheral when available.
    Serial.println("OK");
  } else if (command.length() > 0) {
    Serial.println("ERR:UNKNOWN_COMMAND");
  }
}

void loop() {
  while (Serial.available() > 0) {
    const char incoming = static_cast<char>(Serial.read());
    if (incoming == '\n') {
      lineBuffer.trim();
      handleCommand(lineBuffer);
      lineBuffer = "";
    } else if (lineBuffer.length() < 180) {
      lineBuffer += incoming;
    } else {
      lineBuffer = "";
      Serial.println("ERR:COMMAND_TOO_LONG");
    }
  }
}
