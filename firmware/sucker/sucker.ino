/*
 *  电子吸盘 / 电磁阀 控制器固件
 *  ---------------------------------------------------------------
 *  硬件接线（Arduino Uno / CH340 兼容板）
 *      A0  -> 吸盘（气泵）模块的信号线
 *      A2  -> 电磁阀模块的信号线
 *      两个模块均由外部电源独立供电，并与 Arduino 共地
 *
 *  控制原理
 *      厂家模块接收的是「舵机式 PWM 信号」，不是简单的高低电平：
 *          180 = 开，0 = 关
 *      Uno 上 A0/A2 同时就是数字引脚 14/16，Servo 库可以正常驱动它们。
 *
 *  串口协议
 *      115200 8N1，命令以 \n 结尾，不区分大小写
 *        PICK [ms]          吸盘开；ms > 0 时到点自动关，缺省用 pick_ms
 *        DROP [ms]          吸盘关 + 电磁阀脉冲进气放料，缺省用 drop_ms
 *        STOP               两路全关（急停）
 *        SET pick_ms N      吸取保持时长，0 = 一直吸到 DROP
 *        SET drop_ms N      放气脉冲时长（毫秒）
 *        SET watchdog N     看门狗超时（毫秒），0 = 关闭
 *        MODE servo|digital 输出模式，默认 servo
 *        STATUS             回传一行状态 JSON
 *        PING               心跳，回 PONG
 *      状态变化时固件会主动上报 {"ev":"state",...}
 *
 *  安全设计
 *      1. 上电第一时间把两路都置为「关」，避免一插电就吸住。
 *      2. 看门狗：超过 watchdog 毫秒没有收到任何命令且处于开启态 -> 自动全关，
 *         防止上位机崩溃 / USB 掉线后气泵一直空转烧毁。
 *      3. 打开串口时 Uno 会因 DTR 复位一次，这是正常现象；复位后两路为关。
 */

#include <Servo.h>

// ------------------------------- 引脚 -------------------------------
const uint8_t PIN_PUMP  = A0;   // 吸盘（气泵），数字引脚 14
const uint8_t PIN_VALVE = A2;   // 电磁阀，数字引脚 16
const uint8_t PIN_LED   = 13;   // 板载 LED：亮 = 吸盘工作中

const int ANGLE_OFF = 0;        // 关
const int ANGLE_ON  = 180;      // 开

// ------------------------------- 输出 -------------------------------
enum OutMode { MODE_SERVO, MODE_DIGITAL };

Servo     pumpServo;
Servo     valveServo;
OutMode   outMode = MODE_SERVO;

// ---------------------------- 运行时状态 ----------------------------
bool     pumpOn  = false;
bool     valveOn = false;

uint32_t pickMs     = 0;       // 0 = 一直吸到点「放下」
uint32_t dropMs     = 800;     // 与厂家参考代码一致
uint32_t watchdogMs = 15000;   // 0 = 关闭看门狗

unsigned long pumpOffAt  = 0;  // pickMs > 0 时吸盘的自动关闭时刻
unsigned long valveOffAt = 0;  // 电磁阀放气脉冲的结束时刻
unsigned long lastCmdAt  = 0;  // 最近一次收到有效命令的时刻

// 上报去重
bool lastReportedPump  = false;
bool lastReportedValve = false;
bool stateDirty        = true;

// 行缓冲
char    lineBuf[48];
uint8_t lineLen = 0;

// ============================== 底层输出 ==============================

void writeOut(bool isPump, bool on) {
  if (outMode == MODE_SERVO) {
    Servo &s = isPump ? pumpServo : valveServo;
    s.write(on ? ANGLE_ON : ANGLE_OFF);
  } else {
    digitalWrite(isPump ? PIN_PUMP : PIN_VALVE, on ? HIGH : LOW);
  }
}

void applyOutputs() {
  writeOut(true, pumpOn);
  writeOut(false, valveOn);
  digitalWrite(PIN_LED, pumpOn ? HIGH : LOW);
  stateDirty = true;
}

void setPump(bool on) {
  if (pumpOn == on) return;
  pumpOn = on;
  applyOutputs();
}

void setValve(bool on) {
  if (valveOn == on) return;
  valveOn = on;
  applyOutputs();
}

void stopAll() {
  pumpOffAt  = 0;
  valveOffAt = 0;
  // 直接赋值再统一刷新，避免两次串口上报
  bool changed = pumpOn || valveOn;
  pumpOn  = false;
  valveOn = false;
  applyOutputs();
  if (!changed) stateDirty = true;
}

void setOutMode(OutMode m) {
  // 切换模式前先确保两路都关，避免电平冲突
  pumpOffAt  = 0;
  valveOffAt = 0;
  pumpOn     = false;
  valveOn    = false;

  if (m == MODE_SERVO) {
    pinMode(PIN_PUMP, OUTPUT);
    pinMode(PIN_VALVE, OUTPUT);
    pumpServo.attach(PIN_PUMP);
    valveServo.attach(PIN_VALVE);
  } else {
    pumpServo.detach();
    valveServo.detach();
    pinMode(PIN_PUMP, OUTPUT);
    pinMode(PIN_VALVE, OUTPUT);
    digitalWrite(PIN_PUMP, LOW);
    digitalWrite(PIN_VALVE, LOW);
  }
  outMode = m;
  applyOutputs();
}

// ============================== 状态上报 ==============================

void reportState(bool force) {
  if (!force && !stateDirty && pumpOn == lastReportedPump && valveOn == lastReportedValve) return;
  lastReportedPump  = pumpOn;
  lastReportedValve = valveOn;
  stateDirty        = false;

  Serial.print(F("{\"ev\":\"state\",\"pump\":"));
  Serial.print(pumpOn ? 1 : 0);
  Serial.print(F(",\"valve\":"));
  Serial.print(valveOn ? 1 : 0);
  Serial.print(F(",\"pick_ms\":"));
  Serial.print(pickMs);
  Serial.print(F(",\"drop_ms\":"));
  Serial.print(dropMs);
  Serial.print(F(",\"watchdog\":"));
  Serial.print(watchdogMs);
  Serial.print(F(",\"mode\":\""));
  Serial.print(outMode == MODE_SERVO ? F("servo") : F("digital"));
  Serial.print(F("\",\"up\":"));
  Serial.print(millis());
  Serial.println(F("}"));
}

void ack(const __FlashStringHelper *what) {
  Serial.print(F("{\"ok\":\""));
  Serial.print(what);
  Serial.println(F("\"}"));
}

void err(const __FlashStringHelper *why) {
  Serial.print(F("{\"err\":\""));
  Serial.print(why);
  Serial.println(F("\"}"));
}

// ============================== 动作 ==============================

void doPick(uint32_t ms) {
  lastCmdAt = millis();
  setValve(false);            // 吸取时电磁阀必须关闭，否则真空被破坏
  setPump(true);
  pumpOffAt = (ms > 0) ? (millis() + ms) : 0;
  ack(F("PICK"));
  reportState(true);
}

void doDrop(uint32_t ms) {
  lastCmdAt = millis();
  setPump(false);             // 先停泵
  setValve(true);             // 再开阀进气，破除真空放料
  valveOffAt = millis() + (ms > 0 ? ms : 1);
  ack(F("DROP"));
  reportState(true);
}

void doStop() {
  lastCmdAt = millis();
  stopAll();
  ack(F("STOP"));
  reportState(true);
}

// ============================== 命令解析 ==============================

void upperInPlace(char *s) {
  for (; *s; ++s) if (*s >= 'a' && *s <= 'z') *s -= 32;
}

void handleLine(char *line) {
  char *cmd = strtok(line, " \t");
  if (cmd == NULL) return;

  // 参数里的 key 需要保持小写，所以先记下原始 token 再大写命令
  char *a1 = strtok(NULL, " \t");
  char *a2 = strtok(NULL, " \t");

  upperInPlace(cmd);
  lastCmdAt = millis();

  if (strcmp(cmd, "PICK") == 0) {
    doPick(a1 ? strtoul(a1, NULL, 10) : pickMs);

  } else if (strcmp(cmd, "DROP") == 0) {
    doDrop(a1 ? strtoul(a1, NULL, 10) : dropMs);

  } else if (strcmp(cmd, "STOP") == 0 || strcmp(cmd, "OFF") == 0) {
    doStop();

  } else if (strcmp(cmd, "PING") == 0) {
    Serial.println(F("PONG"));

  } else if (strcmp(cmd, "STATUS") == 0) {
    reportState(true);

  } else if (strcmp(cmd, "MODE") == 0) {
    if (a1 == NULL) { err(F("MODE needs servo|digital")); return; }
    upperInPlace(a1);
    if (strcmp(a1, "SERVO") == 0)        setOutMode(MODE_SERVO);
    else if (strcmp(a1, "DIGITAL") == 0) setOutMode(MODE_DIGITAL);
    else { err(F("MODE needs servo|digital")); return; }
    ack(F("MODE"));
    reportState(true);

  } else if (strcmp(cmd, "SET") == 0) {
    if (a1 == NULL || a2 == NULL) { err(F("SET needs key value")); return; }
    uint32_t v = strtoul(a2, NULL, 10);
    upperInPlace(a1);
    if      (strcmp(a1, "PICK_MS") == 0 || strcmp(a1, "PICK") == 0)     pickMs     = v;
    else if (strcmp(a1, "DROP_MS") == 0 || strcmp(a1, "DROP") == 0)     dropMs     = v;
    else if (strcmp(a1, "WATCHDOG") == 0)                               watchdogMs = v;
    else { err(F("unknown key")); return; }
    ack(F("SET"));
    reportState(true);

  } else if (strcmp(cmd, "HELP") == 0) {
    Serial.println(F("PICK [ms] | DROP [ms] | STOP | SET pick_ms|drop_ms|watchdog N | MODE servo|digital | STATUS | PING"));

  } else {
    err(F("unknown command"));
  }
}

// ============================== 主流程 ==============================

void setup() {
  // 关键：先把输出脚拉到关闭状态，再交给 Servo 库
  pinMode(PIN_PUMP, OUTPUT);
  pinMode(PIN_VALVE, OUTPUT);
  digitalWrite(PIN_PUMP, LOW);
  digitalWrite(PIN_VALVE, LOW);
  pinMode(PIN_LED, OUTPUT);
  digitalWrite(PIN_LED, LOW);

  Serial.begin(115200);

  setOutMode(MODE_SERVO);
  stopAll();

  lastCmdAt = millis();
  Serial.println(F("READY"));
  Serial.println(F("{\"ev\":\"boot\",\"fw\":\"sucker-1.0\",\"pump_pin\":\"A0\",\"valve_pin\":\"A2\"}"));
  reportState(true);
}

void loop() {
  // ---- 串口收行 ----
  while (Serial.available() > 0) {
    char c = (char)Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      lineBuf[lineLen] = '\0';
      if (lineLen > 0) handleLine(lineBuf);
      lineLen = 0;
    } else if (lineLen < sizeof(lineBuf) - 1) {
      lineBuf[lineLen++] = c;
    } else {
      lineLen = 0;              // 行太长，丢弃
      err(F("line too long"));
    }
  }

  // 注意：now 必须在处理完串口之后才取。
  // 若在 loop 开头取值，它会早于 handleLine() 里刚写入的 lastCmdAt，
  // 无符号相减就会下溢成一个极大的数，导致看门狗在刚收到命令的瞬间误触发。
  unsigned long now = millis();

  // ---- 定时关闭 ----
  if (pumpOn && pumpOffAt != 0 && (long)(now - pumpOffAt) >= 0) {
    setPump(false);
    pumpOffAt = 0;
    reportState(true);
  }
  if (valveOn && valveOffAt != 0 && (long)(now - valveOffAt) >= 0) {
    setValve(false);
    valveOffAt = 0;
    reportState(true);
  }

  // ---- 看门狗 ----
  // 用有符号比较再兜一层底：即使 lastCmdAt 意外晚于 now 也绝不会误触发。
  if (watchdogMs > 0 && (pumpOn || valveOn) &&
      (long)(now - lastCmdAt) > (long)watchdogMs) {
    stopAll();
    Serial.println(F("{\"ev\":\"watchdog\"}"));
    reportState(true);
    lastCmdAt = now;
  }

  // ---- 状态变化主动上报 ----
  if (stateDirty) reportState(false);
}
