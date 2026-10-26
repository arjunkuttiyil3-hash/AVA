# AVA serial robot protocol

`SerialRobot` uses UTF-8, newline-terminated commands at the configured baud rate
(default `115200`):

- `G:WAVE`, `G:POINT`, `G:EXPRESSIVE`, `G:BOW`, `G:STOP` control named gestures.
- `S:<text>` forwards up to 160 characters of plain text to a controller that has
  a compatible display or speech module. The Arduino example acknowledges speech
  text over USB serial; it does not generate speech.
- `OK` is the acknowledgement printed by the example. AVA currently does not wait
  for acknowledgements.

Select `robot.mode: serial` and configure `robot.serial_port` in `config/ava.yaml`.
The simulated controller remains available without any hardware.

Upload [the Arduino sketch](./ava_robot/ava_robot.ino) to an ESP32-compatible
Arduino board. Replace the example gesture bodies with the actual motor/servo
driver calls for your robot before connecting actuators.
