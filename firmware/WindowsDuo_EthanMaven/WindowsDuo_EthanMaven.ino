/**
 * ============================================================================
 *  WindowsDuo_EthanMaven  --  笔记本屏幕开合角度传感器固件（二创版）
 * ----------------------------------------------------------------------------
 *  作者     : EthanMaven            GitHub: https://github.com/comreade-123
 *  致敬原作 : WindowsDuo by KaedeharaKazuha1029
 *             https://github.com/KaedeharaKazuha1029/WindowsDuo
 *  许可     : MIT License
 * ----------------------------------------------------------------------------
 *  硬件平台 : Maker-ESP32（ESP32-WROOM-32E），纯 Arduino IDE（无需 PlatformIO）
 *  IMU      : MPU6050（MPU6050_tockn 库）
 *  显示屏   : SSD1306 0.96" 128x64（Adafruit_SSD1306 库）
 *  按键     : GPIO5，另一端接 GND，内部上拉 INPUT_PULLUP，按下为低
 *
 *  接线（OLED 与 MPU6050 并联在同一条 I2C 总线上）:
 *    SDA  -> ESP32 SDA 引脚（默认 GPIO21，见下方 I2C 引脚自动识别）
 *    SCL  -> ESP32 SCL 引脚（默认 GPIO22）
 *    VCC  -> 3V3
 *    GND  -> GND（两块模块必须共地）
 *    MPU6050 AD0 -> GND（地址 0x68）
 *    按键一端 -> GPIO5，另一端 -> GND
 *
 *  固件特性:
 *    1) I2C 引脚自动识别：开机扫描 21/22、23/22、22/21 等常见组合，
 *       自动锁定两个模块实际所在的那一对，并在串口打印出来
 *    2) MPU6050 用 WHO_AM_I(0x75) 校验，杜绝「地址撞上别的器件」
 *    3) 开机静止自动零漂校准（非阻塞，带静止判定与超时保护）
 *    4) 陀螺仪积分 + 加速度计倾角修正的互补滤波，输出平滑的 0-180 度开合角
 *    5) SSD1306 本地 UI：角度大字 + 进度条 + 状态 + 模式（200ms 非阻塞刷新）
 *    6) 按键软件消抖（millis()）：短按切换模式，长按重新零漂校准
 *    7) 串口 115200，每 50ms 输出一行 JSON：
 *       {"angle":45.2,"status":"ok","mode":"default","author":"EthanMaven"}
 *    8) 全代码无 delay() 阻塞主循环（仅初始化阶段允许极短延时）
 *
 *  串口协议:
 *    angle  : number, 0.0 ~ 180.0（保留 1 位小数）
 *    status : ok | calibrating | warming_up | sensor_error
 *    mode   : default | calibrate | debug
 *    以 '#' 开头的行是日志，主机端应跳过
 * ============================================================================
 */

#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <MPU6050_tockn.h>
#include <ArduinoJson.h>
#include <esp_log.h>

/* ---------- 姿态解算数据源选择 ----------
 * true  = 纯陀螺仪积分
 *   绝对单调、无折返、转多少就是多少，但会有约 1 度/分钟的残余漂移。
 *
 * false = 陀螺 + 加速度计互补滤波（当前采用）
 *   加速度计提供绝对姿态参考来抑制漂移，同时用「折返区隔离」避免它引入跳变：
 *     · 加速度计经 atan2 得到的绝对角只在 ±90 度内有效，超出即折返；
 *     · 因此仅当「累积角与参考角方向一致、夹角足够小」时才采信它；
 *     · 一旦夹角过大（进入折返区或剧烈运动），就临时冻结加速度计修正，
 *       完全交给陀螺积分，等转回正常范围后自动恢复。
 *   这样既保留了抗漂移能力，又不会出现「继续转动突然变号」的跳变。
 */
static const bool USE_GYRO_ONLY = false;

/* ---------- 转轴与加速度计参考轴（必须与实物安装一致） ----------
 * HINGE_ACC_AXIS：用哪个加速度计轴做「重力倾斜」参考
 *   0 = X 轴（公式 atan2(-gx, sqrt(gy^2+gz^2))）
 *   1 = Y 轴（公式 atan2(-gy, sqrt(gx^2+gz^2))）
 *   2 = Z 轴（公式 atan2(-gz, sqrt(gx^2+gy^2))）
 *
 * 怎么判断该用哪个轴：串口 debug 模式会输出基准姿态下的加速度各轴读数，
 * 应当选择「基准姿态下该轴接近 ±1g」的那个 —— 它就是测倾角的轴。
 * 选错的后果：信度 conf 恒为 0 → 加速度计修正被永久冻结 → 退化成纯陀螺仪并持续漂移。
 * 现场实测本机基准姿态（板子平放）为 ax=-0.95g、ay=-0.03g、az=-0.03g：
 * X 轴竖直，恰好是 atan2 的奇异点 —— 用 X 轴会让信度恒为 0、修正永久冻结
 * （这正是「漂移一直修不掉」的真正原因）。而 Y、Z 两轴都水平，
 * 用 Z 轴做倾角参考即可正常工作，故选 2。
 * 判断方法：串口 debug 模式输出 ax/ay/az，选择「垂直于竖直轴」的那个轴。 */
static const int HINGE_ACC_AXIS = 2;

/* ---------- 分区解算（按现场实测确定的最终策略） ----------
 * 近零区（>= NEG_HOLD_DEG）：用加速度计校准 -> 抗漂移、绝对准确。
 * 深负区（<  NEG_HOLD_DEG）：纯陀螺仪积分 -> 无折返、无跳变。
 *
 * 为什么这样分：加速度计经 atan2 得到的绝对角只在 ±90 度内有效，
 * 负方向大幅度转动时它会折返，任何基于它的修正都会把角度推向反方向
 * （现场症状就是「继续转动时负值跳到正值」）。而近零区间内它的参考是可靠的，
 * 正好用来抑制陀螺仪的慢漂移。
 *
 * 过渡带阈值取 -8 度而不是贴近零：零位附近的漂移会在 0 ~ -1 度之间来回穿越，
 * 若阈值太靠近零（曾用 -2 度），修正会被频繁冻结，导致近零区漂移无人抑制
 * （实测残留 -1.67 度/分钟的漂移就是这么来的）。 */
static const float NEG_HOLD_DEG = -8.0f;

/* ===========================================================================
 * 一、配置区
 * =========================================================================== */

/* ---------- 按键 ---------- */
static const int PIN_BUTTON = 5;      // 按键输入（内部上拉，按下为低）

/* ---------- I2C 引脚候选（按优先级排列，SDA, SCL） ----------
 * 板子上标着 IIC / SDA / SCL 的排针通常是 GPIO21/GPIO22；
 * 若你的板子把 I2C 引到 23/22，也无需改代码——固件会自动选中有设备的那一对。 */
static const uint8_t I2C_CANDIDATES[][2] = {
  { 21, 22 },   // ESP32 官方默认 I2C（板载 IIC 排针）
  { 23, 22 },   // 部分板子的 I2C 排针
  { 22, 21 },   // 上一对的反接容错
  { 23, 19 },
  { 19, 18 },
  { 18, 19 },
  { 32, 33 },
  { 33, 32 },
  { 25, 26 },
  { 26, 27 }
};
static const uint8_t I2C_CANDIDATE_COUNT = sizeof(I2C_CANDIDATES) / sizeof(I2C_CANDIDATES[0]);

/* ---------- 设备地址 ---------- */
static const uint8_t IMU_ADDR        = 0x68;   // MPU6050（AD0 接 GND）
static const uint8_t OLED_ADDR_MAIN  = 0x3C;   // SSD1306 默认地址
static const uint8_t OLED_ADDR_ALT   = 0x3D;   // 少数模块为 0x3D
static const uint8_t MPU_WHO_AM_I    = 0x75;   // WHO_AM_I 寄存器

/* ---------- OLED ---------- */
static const int    OLED_WIDTH  = 128;
static const int    OLED_HEIGHT = 64;
static const int8_t OLED_RESET  = -1;          // 模块自带复位，无需 MCU 控制

/* ---------- 串口与调度周期 ---------- */
static const uint32_t SERIAL_BAUD          = 115200;
static const uint32_t OLED_UPDATE_INTERVAL = 200;    // OLED 刷新周期（ms）
static const uint32_t SERIAL_SEND_INTERVAL = 50;     // JSON 输出周期（ms，20Hz）
static const uint32_t IMU_SAMPLE_INTERVAL  = 5;      // IMU 采样间隔（ms，约 200Hz）
static const uint32_t I2C_RETRY_INTERVAL   = 2000;   // 设备掉线后的重试间隔（ms）

/* ---------- 角度与滤波 ---------- */
/* ---------- 角度符号（安装方向适配） ----------
 * 决定「哪个转动方向输出正角度」：
 *   +1.0f = 与固件的默认轴方向一致
 *   -1.0f = 翻转（当实物测试发现正负值与预期相反时改这里）
 * 现场判定方法：把板子朝"应当出现透视拉伸"的方向转动，串口里的 angle 必须为正。 */
static const float HINGE_SIGN = -1.0f;

/* ---------- 角度范围（有符号） ----------
 * 固件输出「相对开机基准角」的有符号偏差：
 *   正角度 = 一侧开合，负角度 = 另一侧开合
 * PC 端据此决定是否做透视拉伸（例如正角度 0~90 度才拉伸，负值保持清晰）。
 * 修改 HINGE_SIGN 或安装朝向可以翻转正负方向。 */
static const float ANGLE_MIN_DEG = -180.0f;    // 负向极限
static const float ANGLE_MAX_DEG = 180.0f;     // 正向极限
static const float COMP_ALPHA_STILL  = 0.80f; // 静止时加速度计权重 ≈ 20%（时间常数约 0.25s）
static const float COMP_ALPHA_MOVING = 0.95f; // 运动时 ≈ 5%，优先保持跟手性
static const float ANGLE_LPF_ALPHA   = 0.35f;  // 输出低通系数
static const float STILL_ABS_DPS     = 2.5f;   // 判定静止的角速度阈值（度/秒）
static const float STILL_BASE_TRACK  = 0.004f; // 静止时基准角跟随系数（抑制积分漂移）
static const float RAD_TO_DEG_F = 57.29578f;
static const float DEG_TO_RAD_F = 0.01745329f;

/* ---------- 零漂校准 ----------
 * 采样窗口越长，零偏估计越准，后续漂移越小。
 *
 * 判定「是否静止」只用**波动**（标准差），不要用均值：
 *   均值 meanAbsGyro 就是陀螺零偏本身（GY-521 典型 5 dps 左右），
 *   板子静止时它当然非零。早期版本拿均值当运动判据，
 *   导致静止的板子永远被判为「在动」，校准无限重试 —— 已修正。
 * 方差阈值 25 对应每轴标准差约 2.9 dps，足以区分
 *   「静止（实测 varSum≈0.1）」与「手持微颤（varSum 显著增大）」。 */
static const uint32_t CALIB_WINDOW_MS    = 5000;   // 静止采样窗口（约 1000 样本）
static const uint32_t CALIB_TIMEOUT_MS   = 12000;  // 超时保护
static const float    CALIB_VAR_MAX      = 25.0f;  // 静止判定：三轴角速度方差上限
static const float    CALIB_GYRO_MAX_DPS = 60.0f;  // 均值仅做量级保护（零偏不会这么大）

/* ---------- 按键时序 ---------- */
static const uint32_t BTN_DEBOUNCE_MS = 30;    // 软件消抖
static const uint32_t BTN_LONG_MS     = 800;   // 长按阈值
static const uint32_t BTN_SHORT_MIN_MS = 50;   // 短按最短有效时长

/* ---------- 文本常量 ---------- */
static const char TXT_STATUS_OK[]      = "ok";
static const char TXT_STATUS_CALIB[]   = "calibrating";
static const char TXT_STATUS_WARMUP[]  = "warming_up";
static const char TXT_STATUS_ERROR[]   = "sensor_error";
static const char TXT_MODE_DEFAULT[]   = "default";
static const char TXT_MODE_CALIBRATE[] = "calibrate";
static const char TXT_MODE_DEBUG[]     = "debug";
static const char TXT_AUTHOR[]         = "EthanMaven";

/* ===========================================================================
 * 二、枚举与全局对象
 * =========================================================================== */

enum SystemStatus : uint8_t {
  STATUS_WARMING_UP = 0,
  STATUS_CALIBRATING,
  STATUS_OK,
  STATUS_SENSOR_ERROR
};

enum WorkMode : uint8_t {
  MODE_DEFAULT = 0,
  MODE_CALIBRATE,
  MODE_DEBUG
};

enum ButtonState : uint8_t {
  BTN_IDLE = 0,
  BTN_DEBOUNCE_DOWN,
  BTN_PRESSED,
  BTN_DEBOUNCE_UP
};

struct Vector3 {
  float x;
  float y;
  float z;
};

Adafruit_SSD1306 oled(OLED_WIDTH, OLED_HEIGHT, &Wire, OLED_RESET);
MPU6050 imu(Wire);

/* ===========================================================================
 * 三、运行期状态
 * =========================================================================== */

static bool         oledReady    = false;
static bool         imuReady     = false;
static bool         i2cStarted   = false;
static uint8_t      i2cSdaPin    = I2C_CANDIDATES[0][0];
static uint8_t      i2cSclPin    = I2C_CANDIDATES[0][1];
static uint8_t      oledAddress  = OLED_ADDR_MAIN;
static SystemStatus sysStatus    = STATUS_WARMING_UP;
static WorkMode     workMode     = MODE_DEFAULT;

/* 姿态解算 */
static float pitchDeg      = 0.0f;   // 陀螺积分累积相对转角（开机 = 0，无界，不取模！）
static float virtualAngle  = 0.0f;   // 有符号输出角（应用 HINGE_SIGN 后）
static float angleDeg      = 0.0f;   // 对外输出（低通平滑后）
static float gyroRateDps   = 0.0f;   // 开合方向角速度（度/秒，已扣零偏）
static float imuPitchBaseDeg = 0.0f; // 累积角为 0 时所对应的加速度计绝对俯仰角
static bool  accelFrozen    = false; // 折返区隔离：true 时暂停加速度计修正
static float dbgDev         = 0.0f;  // 诊断：当前修正偏差（度）
static float dbgConf        = 0.0f;  // 诊断：加速度计倾角信度 0..1
static float dbgCorr        = 0.0f;  // 诊断：本次修正量（度）
static bool  calibSucceeded = false; // 开机零偏校准是否已成功（失败会自动重试）

/* 零漂校准 */
static float    gyroBiasX = 0.0f, gyroBiasY = 0.0f, gyroBiasZ = 0.0f;
static float    baselinePitchDeg = 0.0f;
static bool     calibActive      = false;
static uint32_t calibStartMs     = 0;
static uint32_t calibLastMs      = 0;
static uint32_t calibSamples     = 0;
static double   calibSumGx = 0.0, calibSumGy = 0.0, calibSumGz = 0.0;
static double   calibSumSqGx = 0.0, calibSumSqGy = 0.0, calibSumSqGz = 0.0;
static double   calibSumPitch = 0.0;

/* 采样数据 */
static Vector3 accelG   = { 0.0f, 0.0f, 0.0f };   // 单位 g
static Vector3 gyroDps  = { 0.0f, 0.0f, 0.0f };   // 度/秒

/* 非阻塞调度 */
static uint32_t lastOledMs   = 0;
static uint32_t lastSendMs   = 0;
static uint32_t lastImuMs    = 0;
static uint32_t lastImuRetryMs = 0;

/* 按键 */
static ButtonState btnState      = BTN_IDLE;
static uint32_t    btnEdgeMs     = 0;
static uint32_t    btnPressedMs  = 0;
static bool        btnLongLatched = false;
static uint32_t    shortPressCount = 0;
static uint32_t    longPressCount  = 0;
static bool        buttonEnabled   = true;   // 按键可用性（悬空检测会置 false）

/* OLED 渲染缓存（内容未变则不重绘） */
static int oledCachedAngleInt = -1;
static int oledCachedBarPct   = -1;
static int oledCachedMode     = -1;
static int oledCachedStatus   = -1;
static int oledCachedCalibPct = -1;

/* ===========================================================================
 * 四、工具函数
 * =========================================================================== */

static float clampFloat(float v, float lo, float hi) {
  if (v < lo) return lo;
  if (v > hi) return hi;
  return v;
}

/** 角度归一化到 [-180, 180) */
static float wrapDeg180(float deg) {
  while (deg >= 180.0f)  deg -= 360.0f;
  while (deg < -180.0f)  deg += 360.0f;
  return deg;
}

static const char *statusText() {
  switch (sysStatus) {
    case STATUS_CALIBRATING:  return TXT_STATUS_CALIB;
    case STATUS_OK:           return TXT_STATUS_OK;
    case STATUS_SENSOR_ERROR: return TXT_STATUS_ERROR;
    case STATUS_WARMING_UP:
    default:                  return TXT_STATUS_WARMUP;
  }
}

static const char *modeText() {
  switch (workMode) {
    case MODE_CALIBRATE: return TXT_MODE_CALIBRATE;
    case MODE_DEBUG:     return TXT_MODE_DEBUG;
    case MODE_DEFAULT:
    default:             return TXT_MODE_DEFAULT;
  }
}

/** OLED 上的模式短名 */
static const char *modeTextShort() {
  switch (workMode) {
    case MODE_CALIBRATE: return "CAL";
    case MODE_DEBUG:     return "DBG";
    case MODE_DEFAULT:
    default:             return "STD";
  }
}

/**
 * 由加速度计重力矢量求俯仰角（度）与「可信度」。
 *
 * pitch = atan2(-gx, sqrt(gy^2+gz^2))  —— 注意该式在 ±90° 处会折返
 * （水平分量趋近 0 时，角度看起来往回走），这是 atan2 的固有奇异性。
 * 因此同时返回可信度 conf = horizontal/|g|：
 *   绕转轴（X）接近竖直时 conf -> 0，此时角度解算不可靠，应完全交给陀螺仪。
 * 没有这个权重时，快速转动会把陀螺仪积分出的负角度被折返的加速度计值拉回正值
 * （现场表现：负值只闪现零点几秒就变正）。
 */
static float pitchFromAccel(const Vector3 &a, float *confidence) {
  /* 支持三种安装朝向，由 HINGE_ACC_AXIS 选择（见文件顶部说明）。
     各自取「垂直于该轴的平面内重力分量」作为水平分量：
       水平分量越小 → 该轴越接近竖直 → atan2 越不可靠 → 信度越低。 */
  float axial, horizontal;
  if (HINGE_ACC_AXIS == 1) {
    axial      = a.y;
    horizontal = sqrtf(a.x * a.x + a.z * a.z);
  } else if (HINGE_ACC_AXIS == 2) {
    axial      = a.z;
    horizontal = sqrtf(a.x * a.x + a.y * a.y);
  } else {
    axial      = a.x;
    horizontal = sqrtf(a.y * a.y + a.z * a.z);
  }

  const float magnitude = sqrtf(a.x * a.x + a.y * a.y + a.z * a.z);
  if (confidence != NULL) {
    const float raw = (magnitude > 0.1f) ? (horizontal / magnitude) : 0.0f;
    /* 低于 0.17（约 80 度倾角）开始衰减，低于 0.05（约 87 度）基本不用 */
    *confidence = clampFloat((raw - 0.05f) / 0.12f, 0.0f, 1.0f);
  }
  return atan2f(-axial, horizontal) * RAD_TO_DEG_F;
}

/** 直接探测某地址上是否有器件应答（真 ACK，不是库的返回值） */
static bool i2cPing(uint8_t addr) {
  Wire.beginTransmission(addr);
  return (Wire.endTransmission() == 0);
}

/**
 * 静默探测：与 i2cPing 相同，但临时关闭 I2C 驱动的错误日志。
 * 用途：设备缺失时周期性重试会产生大量 "I2C hardware NACK" 日志，
 *       会把串口里真正有用的诊断信息淹没。日志级别 0=NONE，1=ERROR。
 */
static bool i2cPingQuiet(uint8_t addr) {
  esp_log_level_set("i2c.master", static_cast<esp_log_level_t>(0));
  const bool ok = i2cPing(addr);
  esp_log_level_set("i2c.master", static_cast<esp_log_level_t>(1));
  return ok;
}

/** 统一刷新系统状态：传感器故障 > 校准中 > 预热 > 正常 */
static void refreshStatus() {
  if (!imuReady || !oledReady) {
    sysStatus = STATUS_SENSOR_ERROR;
    return;
  }
  if (calibActive) {
    sysStatus = STATUS_CALIBRATING;
    return;
  }
  /* 校准还没成功（在等板子静止）时如实报 warming_up，
     让主机端知道当前角度还没准备好，而不是误报 ok。 */
  if (!calibSucceeded) {
    sysStatus = STATUS_WARMING_UP;
    return;
  }
  if (sysStatus == STATUS_SENSOR_ERROR || sysStatus == STATUS_CALIBRATING ||
      sysStatus == STATUS_WARMING_UP) {
    sysStatus = STATUS_OK;
  }
}

/* ===========================================================================
 * 五、前向声明
 * =========================================================================== */

static void   startCalibration(const char *reason);
static void   finishCalibration(bool accepted);
static void   updateCalibration(uint32_t now);
static void   sampleImu(uint32_t now);
static void   updateAttitude(uint32_t now);
static void   updateButton(uint32_t now);
static void   updateOled(uint32_t now, bool force);
static void   drawMainScreen();
static void   drawCalibrationScreen(uint32_t now);
static void   sendJsonLine();
static int    calibProgressPercent();
static void   nextMode();
static void   pollSerialCommand(uint32_t now);
static void   handleCommand(const char *cmd);
static bool   initOled(uint8_t addr);
static bool   initImu();
static void   oledHealthCheck(uint32_t now);
static void   calibrationWatchdog(uint32_t now);
static bool   selectI2cPins();
static void   recoverDevices(uint32_t now);

/* ===========================================================================
 * 六、I2C 引脚识别与外设初始化
 * =========================================================================== */

/**
 * 依次尝试候选引脚组合，选中「两个模块都能应答」的那一对。
 * 这是本固件对现场接线差异的兼容手段：无论 I2C 接在 21/22 还是 23/22，
 * 都不需要改代码，固件会自己找到并在串口打印实际使用的引脚。
 */
static bool selectI2cPins() {
  Serial.println(F("# 正在识别 I2C 引脚 ..."));

  for (uint8_t i = 0; i < I2C_CANDIDATE_COUNT; i++) {
    const uint8_t sda = I2C_CANDIDATES[i][0];
    const uint8_t scl = I2C_CANDIDATES[i][1];

    if (i2cStarted) {
      Wire.end();
      i2cStarted = false;
    }
    Wire.begin(sda, scl);
    i2cStarted = true;
    Wire.setClock(100000L);
    delay(3);

    const bool hasImu  = i2cPingQuiet(IMU_ADDR);
    const bool hasOled = i2cPingQuiet(OLED_ADDR_MAIN) || i2cPingQuiet(OLED_ADDR_ALT);
    const bool hasAny  = i2cPingQuiet(IMU_ADDR) || i2cPingQuiet(OLED_ADDR_MAIN) ||
                         i2cPingQuiet(OLED_ADDR_ALT);

    Serial.print(F("#   SDA=GPIO"));
    Serial.print(sda);
    Serial.print(F(" SCL=GPIO"));
    Serial.print(scl);
    Serial.print(F(" -> MPU6050="));
    Serial.print(hasImu ? F("ACK") : F("-"));
    Serial.print(F(" OLED="));
    Serial.println(hasOled ? F("ACK") : F("-"));

    if (hasImu && hasOled) {
      i2cSdaPin = sda;
      i2cSclPin = scl;
      Serial.print(F("# I2C 锁定: SDA=GPIO"));
      Serial.print(sda);
      Serial.print(F(" SCL=GPIO"));
      Serial.println(scl);
      return true;
    }
    if (hasAny && i == 0) {
      /* 第一个候选上有设备但不全：记录下来，继续找更完整的组合 */
      i2cSdaPin = sda;
      i2cSclPin = scl;
    }
  }

  /* 没有找到「两个都全」的组合：退回第一个候选，靠运行时重试补救 */
  if (i2cStarted) {
    Wire.end();
    i2cStarted = false;
  }
  i2cSdaPin = I2C_CANDIDATES[0][0];
  i2cSclPin = I2C_CANDIDATES[0][1];
  Wire.begin(i2cSdaPin, i2cSclPin);
  i2cStarted = true;
  Wire.setClock(100000L);
  Wire.setTimeOut(50);
  Serial.println(F("# 警告: 未找到两个模块都在线的引脚组合，已回退到 GPIO21/22"));
  return false;
}

/** 初始化 MPU6050：先探测 ACK，再用 WHO_AM_I 校验器件身份 */
static bool initImu() {
  if (!i2cPingQuiet(IMU_ADDR)) {
    return false;
  }
  imu.begin();                 // tockn：唤醒 + 复位 + 设置量程（±2g / ±250dps）
  delay(10);

  const uint8_t who = imu.readMPU6050(MPU_WHO_AM_I);
  Serial.print(F("# MPU6050 WHO_AM_I=0x"));
  Serial.println(who, HEX);
  if (who != 0x68 && who != 0x69 && who != 0x70 && who != 0x71 && who != 0x73) {
    Serial.println(F("# MPU6050 身份校验失败：0x68 上的器件不是 MPU6050/6500/9250"));
    return false;
  }

  /* 采样率 200Hz + DLPF 21Hz（抑制振动噪声，寄存器与 tockn 兼容） */
  imu.writeMPU6050(0x19, 0x04);   // SMPLRT_DIV
  imu.writeMPU6050(0x1A, 0x04);   // CONFIG: DLPF_CFG=4
  delay(5);
  return true;
}

/** 初始化 SSD1306：先真实探测地址，再交给库初始化（探测失败不刷日志） */
static bool initOled(uint8_t addr) {
  if (!i2cPingQuiet(addr)) {
    return false;
  }
  if (!oled.begin(SSD1306_SWITCHCAPVCC, addr)) {
    return false;
  }
  oledAddress = addr;
  return true;
}

/** 设备掉线后的补救：按需重新初始化 OLED / MPU6050 */
static void recoverDevices(uint32_t now) {
  if (oledReady && imuReady) {
    return;
  }
  if (now - lastImuRetryMs < I2C_RETRY_INTERVAL) {
    return;
  }
  lastImuRetryMs = now;

  if (!oledReady) {
    /* OLED 可能上电稍慢，或地址为 0x3D */
    if (initOled(oledAddress)) {
      oledReady = true;
      Serial.println(F("# OLED 已接入并初始化"));
    } else if (oledAddress != OLED_ADDR_ALT && initOled(OLED_ADDR_ALT)) {
      oledReady = true;
      Serial.println(F("# OLED 已接入（地址 0x3D）"));
    }
    refreshStatus();
  }

  if (!imuReady) {
    if (initImu()) {
      imuReady = true;
      Serial.println(F("# MPU6050 已接入并初始化"));
      refreshStatus();
    }
  }
}

/* ===========================================================================
 * 七、初始化
 * =========================================================================== */

void setup() {
  Serial.begin(SERIAL_BAUD);
  delay(50);                    // 等 USB 转串口稳定，避免开机日志丢失

  Serial.println();
  Serial.println(F("# ================================================"));
  Serial.println(F("# WindowsDuo_EthanMaven  booting ..."));
  Serial.print(F("# build: "));
  Serial.print(F(__DATE__));
  Serial.print(F(" "));
  Serial.println(F(__TIME__));

  pinMode(PIN_BUTTON, INPUT_PULLUP);

  /* 1) 自动识别 I2C 引脚 */
  selectI2cPins();
  Wire.setTimeOut(50);

  /* 2) 初始化两个设备 */
  if (i2cPingQuiet(OLED_ADDR_MAIN)) {
    oledReady = initOled(OLED_ADDR_MAIN);
  } else if (i2cPingQuiet(OLED_ADDR_ALT)) {
    oledReady = initOled(OLED_ADDR_ALT);
  } else {
    oledReady = initOled(OLED_ADDR_MAIN);   // 再试一次，失败就交给运行时重试
  }
  Serial.println(oledReady ? F("# OLED 初始化成功") : F("# !!! OLED 未应答（检查 SDA/SCL/VCC/GND）"));

  imuReady = initImu();
  Serial.println(imuReady ? F("# MPU6050 初始化成功") : F("# !!! MPU6050 未应答（检查 AD0->GND / 供电）"));

  /* 3) 自检汇总 + 首次自检画面 */
  Serial.print(F("# 自检: OLED="));
  Serial.print(oledReady ? F("OK") : F("FAIL"));
  Serial.print(F("  MPU6050="));
  Serial.println(imuReady ? F("OK") : F("FAIL"));
  Serial.print(F("# I2C: SDA=GPIO"));
  Serial.print(i2cSdaPin);
  Serial.print(F(" SCL=GPIO"));
  Serial.println(i2cSclPin);
  Serial.println(F("# ================================================"));

  if (oledReady) {
    oled.clearDisplay();
    oled.setTextSize(1);
    oled.setTextColor(SSD1306_WHITE);
    oled.setCursor(0, 0);
    oled.println(F("WinDuo-EthanMaven"));
    oled.drawFastHLine(0, 10, OLED_WIDTH, SSD1306_WHITE);
    oled.setCursor(0, 14);
    oled.print(F("I2C SDA"));
    oled.print(i2cSdaPin);
    oled.print(F(" SCL"));
    oled.println(i2cSclPin);
    oled.setCursor(0, 26);
    oled.print(F("OLED    0x"));
    oled.println(oledAddress, HEX);
    oled.setCursor(0, 38);
    oled.print(F("MPU6050 "));
    oled.println(imuReady ? F("OK") : F("FAIL"));
    oled.setCursor(0, 52);
    oled.print(F("BTN GPIO"));
    oled.print(PIN_BUTTON);
    oled.display();
  }

  /* 4) 开机静止自动零漂校准（非阻塞） */
  if (imuReady) {
    sysStatus = STATUS_WARMING_UP;
  } else {
    sysStatus = STATUS_SENSOR_ERROR;
  }
  startCalibration("boot");

  /* 用与 400kHz 兼容的时钟跑起来（两个设备都确认可用时） */
  if (imuReady && oledReady) {
    Wire.setClock(400000L);
  }

  lastOledMs = millis();
  updateOled(millis(), true);
}

/* ===========================================================================
 * 八、主循环（全部非阻塞）
 * =========================================================================== */

void loop() {
  const uint32_t now = millis();

  pollSerialCommand(now);   // 串口命令（cal / mode= / status）
  sampleImu(now);           // IMU 采样（约 200Hz）
  updateCalibration(now);   // 零漂校准状态机
  updateAttitude(now);      // 互补滤波 + 限幅 + 低通
  updateButton(now);        // 按键消抖与短按/长按
  recoverDevices(now);      // 设备掉线补救
  calibrationWatchdog(now);// 校准失败自动重试（否则坐标基准一直是错的）
  oledHealthCheck(now);     // OLED 掉帧/掉线监测与恢复
  updateOled(now, false);   // OLED 非阻塞刷新

  if (now - lastSendMs >= SERIAL_SEND_INTERVAL) {
    lastSendMs = now;
    sendJsonLine();
  }
}

/* ===========================================================================
 * 九、IMU 采样
 * =========================================================================== */

static void sampleImu(uint32_t now) {
  if (!imuReady) {
    return;                 // 由 recoverDevices() 负责重新初始化
  }
  if (now - lastImuMs < IMU_SAMPLE_INTERVAL) {
    return;
  }
  lastImuMs = now;

  imu.update();             // tockn：一次性刷新原始值与换算后的物理量

  /* 全 0 通常意味着 I2C 通信已经失效（tockn 不返回错误码） */
  const int32_t rawSum = (int32_t)abs(imu.getRawAccX()) + (int32_t)abs(imu.getRawAccY()) +
                         (int32_t)abs(imu.getRawAccZ()) + (int32_t)abs(imu.getRawGyroX()) +
                         (int32_t)abs(imu.getRawGyroY()) + (int32_t)abs(imu.getRawGyroZ());
  if (rawSum == 0) {
    imuReady = false;
    refreshStatus();
    return;
  }

  /* tockn 已换算好单位：加速度 g、角速度 度/秒 */
  accelG.x = imu.getAccX();
  accelG.y = imu.getAccY();
  accelG.z = imu.getAccZ();

  gyroDps.x = imu.getGyroX();
  gyroDps.y = imu.getGyroY();
  gyroDps.z = imu.getGyroZ();
}

/* ===========================================================================
 * 十、零漂校准
 * =========================================================================== */

static void startCalibration(const char *reason) {
  calibActive   = true;
  calibSucceeded = false;   // 新一轮校准开始，成功前不输出累积角
  calibStartMs  = millis();
  calibLastMs   = calibStartMs;
  calibSamples  = 0;
  calibSumGx = calibSumGy = calibSumGz = 0.0;
  calibSumSqGx = calibSumSqGy = calibSumSqGz = 0.0;
  calibSumPitch = 0.0;
  lastImuMs = 0;            // 校准期间立即采样，不受节流限制
  /* 校准期间保持 default 模式：早期版本会切到 calibrate 模式并让用户以为
     "角度卡住了"（实际上校准期间不更新角度是为了不让半成品的零偏污染读数）。 */
  workMode = MODE_DEFAULT;
  refreshStatus();
  Serial.print(F("# 开始零漂校准: "));
  Serial.println(reason);
}

static void updateCalibration(uint32_t now) {
  if (!calibActive) {
    return;
  }
  if (!imuReady) {
    calibActive = false;
    refreshStatus();
    return;
  }

  if (now - calibLastMs >= IMU_SAMPLE_INTERVAL) {
    calibLastMs = now;
    calibSamples++;
    calibSumGx += gyroDps.x;
    calibSumGy += gyroDps.y;
    calibSumGz += gyroDps.z;
    calibSumSqGx += (double)gyroDps.x * gyroDps.x;
    calibSumSqGy += (double)gyroDps.y * gyroDps.y;
    calibSumSqGz += (double)gyroDps.z * gyroDps.z;
    calibSumPitch += pitchFromAccel(accelG, NULL);
  }

  if (now - calibStartMs >= CALIB_WINDOW_MS && calibSamples >= 100) {
    const double n  = (double)calibSamples;
    const double mx = calibSumGx / n;
    const double my = calibSumGy / n;
    const double mz = calibSumGz / n;

    double varSum = 0.0;
    varSum += (calibSumSqGx / n) - (mx * mx);
    varSum += (calibSumSqGy / n) - (my * my);
    varSum += (calibSumSqGz / n) - (mz * mz);

    const float meanAbs = fabsf((float)mx) + fabsf((float)my) + fabsf((float)mz);
    const bool  accepted = (varSum <= CALIB_VAR_MAX) && (meanAbs <= CALIB_GYRO_MAX_DPS);

    /* 打印判定依据：阈值不合适时表现为「校准一直被拒绝」，
       有这行数据就能直接看出是噪声大还是板子真在动。 */
    Serial.print(F("# 校准判定 varSum="));
    Serial.print((float)varSum, 1);
    Serial.print(F(" (限"));
    Serial.print(CALIB_VAR_MAX, 0);
    Serial.print(F(") meanAbsGyro="));
    Serial.print(meanAbs, 2);
    Serial.print(F(" (限"));
    Serial.print(CALIB_GYRO_MAX_DPS, 1);
    Serial.print(F(") 样本="));
    Serial.println(calibSamples);

    finishCalibration(accepted);
    return;
  }

  if (now - calibStartMs >= CALIB_TIMEOUT_MS) {
    finishCalibration(false);
  }
}

static void finishCalibration(bool accepted) {
  if (accepted && calibSamples > 0) {
    const double n = (double)calibSamples;
    gyroBiasX = (float)(calibSumGx / n);
    gyroBiasY = (float)(calibSumGy / n);
    gyroBiasZ = (float)(calibSumGz / n);
    baselinePitchDeg  = (float)(calibSumPitch / n);
    imuPitchBaseDeg   = baselinePitchDeg;   // 累积角 0 对应的绝对俯仰角
    /* 累积角归零：开机姿态即 0 度，之后由陀螺积分自由累积（不取模） */
    pitchDeg        = 0.0f;
    virtualAngle    = 0.0f;
    angleDeg        = 0.0f;
    accelFrozen     = false;
    calibSucceeded  = true;
    Serial.print(F("# 校准完成 零偏(dps)="));
    Serial.print(gyroBiasX, 3);
    Serial.print(F(","));
    Serial.print(gyroBiasY, 3);
    Serial.print(F(","));
    Serial.print(gyroBiasZ, 3);
    Serial.print(F("  基准角="));
    Serial.println(baselinePitchDeg, 2);
  } else {
    Serial.println(F("# 校准被拒绝：设备在动，2 秒后自动重试"));
  }

  calibActive = false;
  oledCachedCalibPct = -1;
  oledCachedStatus   = -1;
  refreshStatus();
}

/**
 * 校准监督：如果开机校准没有成功（用户当时在动板子），
 * 每 2 秒自动重试一次，直到成功为止。
 * 这是必要的：imuPitchBaseDeg 只有在校准成功时才被写入，
 * 校准一直不成功就会让坐标系基准错误，表现为「没动板子读数却是负的」。
 */
static void calibrationWatchdog(uint32_t now) {
  static uint32_t lastRetryMs = 0;

  if (calibSucceeded || calibActive || !imuReady) {
    return;
  }
  if (now - lastRetryMs < 2000) {
    return;
  }
  lastRetryMs = now;
  startCalibration("auto-retry (waiting for a still board)");
}

/* ===========================================================================
 * 十一、姿态解算（互补滤波）
 * =========================================================================== */

static void updateAttitude(uint32_t now) {
  static uint32_t lastMs = 0;

  const uint32_t dtMs = now - lastMs;
  if (dtMs < IMU_SAMPLE_INTERVAL) {
    return;
  }
  lastMs = now;
  const float dtSec = (float)dtMs / 1000.0f;

  /* 扣零偏；绕转轴（Y 轴）的角速度就是开合角速度 */
  const float gx = gyroDps.x - gyroBiasX;
  const float gy = gyroDps.y - gyroBiasY;
  const float gz = gyroDps.z - gyroBiasZ;
  gyroRateDps = gy;

  /* ==========================================================================
   * 角度解算架构（这里反复踩过坑，改动前请读完）
   * --------------------------------------------------------------------------
   * 采用「无界累积 + 最短路径修正」：
   *   1) pitchDeg = 陀螺积分得到的累积相对转角（开机 = 0，**不取模**）；
   *   2) 加速度计提供绝对姿态参考，用 imuPitchBaseDeg 把它换算成同一坐标系，
   *      偏差一律用 wrapDeg180 取最短路径，因此累积角无界也不影响正确性；
   *   3) 只在「信度足够 + 方向一致」时修正，见下方第 2 步的两个条件。
   *
   * 两个关键教训（都造成过现场可见的跳变）：
   *   a) 千万不要对 pitchDeg 做 wrapDeg180：一旦越过 ±180 度就被折回反方向，
   *      再叠加加速度计修正会把角度猛地推向正区 ——
   *      现场表现是「继续转动时从负值跳到正值」；
   *   b) 加速度计参考在接近 ±90 度时会折返，必须按信度 accConf 降低权重，
   *      否则快速转动的大角度会被折返的参考值拉回（负值只闪现零点几秒）。
   * ========================================================================== */

  /* 1) 陀螺积分累积。
        刻意不取 ±180 模：pitchDeg 的语义是「相对开机姿态的累积转角」，
        多圈/大幅度转动必须能继续累积。一旦在这里 wrap，越界瞬间会被折回
        反方向，再叠加加速度计修正就会把角度猛地推向正区 ——
        现场表现正是「继续转动时从负值跳到正值」。

        只有在零偏校准成功后才允许累积：校准时会把累积角与坐标基准一起归零，
        在校准成功之前累积出的值是建立在错误基准上的，没有意义。 */
  if (calibSucceeded) {
    pitchDeg += gyroRateDps * dtSec;
  }

  /* 2) 加速度计修正。
        两个必要条件：
          a) 按倾角信度 accConf 缩放权重：pitchFromAccel 在接近 ±90 度时折返，
             此时必须少采信加速度计，否则快速转动的大角度会被拉回；
          b) 方向一致性检查：只有当加速度计参考与当前累积角的方向一致时
             （夹角 < 90 度）才修正。这杜绝了「参考方向相反 → 虚假大修正
             → 角度被推向另一侧」这一类跳变。
        差值一律用 wrapDeg180 取最短路径，因此累积角是无界也不影响正确性。 */
  const float accelMag = sqrtf(accelG.x * accelG.x + accelG.y * accelG.y + accelG.z * accelG.z);
  const bool  accelOk  = (accelMag > 0.75f) && (accelMag < 1.25f);
  const bool  stillNow = (fabsf(gx) + fabsf(gy) + fabsf(gz)) < STILL_ABS_DPS;
  float accConf = 0.0f;

  /* 分区解算要在修正之前判断当前所处区域，所以这里先更新一次有符号角 */
  virtualAngle = HINGE_SIGN * pitchDeg;

  if (accelOk && !USE_GYRO_ONLY) {
    const float accelPitch = pitchFromAccel(accelG, &accConf);
    /* 累积角与加速度计绝对角的偏差（最短路径） */
    const float dev = wrapDeg180((imuPitchBaseDeg + pitchDeg) - accelPitch);

    /* ------------------------------------------------------------------
     * 分区解算（现场实测后确定的最终策略）
     * ----------------------------------------------------------------
     *   正值区（含 0 ~ NEG_HOLD_DEG 过渡带）：加速度计校准，抗漂移；
     *   负值区（< NEG_HOLD_DEG）            ：纯陀螺仪，避免折返把角度推向反方向。
     * 另外仍要求「倾角信度足够 + 偏差在 90 度以内」，
     * 防止在折返区或剧烈运动时被不可信的参考带偏。 */
    const bool positiveZone = (virtualAngle >= NEG_HOLD_DEG);
    const bool accelTrusted = positiveZone && (accConf > 0.25f) && (fabsf(dev) < 90.0f);

    dbgDev  = dev;
    dbgConf = accConf;

    if (accelTrusted) {
      if (accelFrozen) {
        accelFrozen = false;
        Serial.println(F("# 回到正值区，加速度计重新接管抗漂移"));
      }
      /* 修正力度：直接给出收敛系数，而不是让 alpha 层层相乘。
         早期写法 base = (1-alpha)*accConf 在 alpha=0.8~0.95 时只剩 5%~20%，
         再乘信度后动量太小，修正量落在加速度计噪声（±0.3 度）里打转，
         表现为 corr 正负振荡、漂移压不住。
         这里用固定收敛系数 0.15：在 20Hz 下时间常数约 0.33 秒，
         足以把慢漂移压到可忽略，又远小于手动转动的动态。 */
      const float gain = stillNow ? 0.15f : 0.05f;   // 静止时强一些，运动时轻一些
      dbgCorr = -gain * accConf * dev;
      pitchDeg += dbgCorr;
    } else {
      dbgCorr = 0.0f;              // 被冻结：本帧没有修正
      if (!accelFrozen) {
        accelFrozen = true;
        Serial.print(F("# 进入深负区/参考不可信，暂停加速度计修正 (angle="));
        Serial.print(virtualAngle, 1);
        Serial.print(F(" conf="));
        Serial.print(accConf, 2);
        Serial.print(F(" dev="));
        Serial.print(dev, 1);
        Serial.println(F(")"));
      }
    }
  }

  /* 3) 有符号相对角 = 累积转角 × 符号。
        这里同样**不能** wrapDeg180：累积角本来就是无界的，
        包一层取模会把超过 ±180 的部分折回反方向（用户实测过这个跳变）。
        注意本行必须放在加速度计修正之后，这样输出用的是修正后的最新值；
        修正之前已经先算过一次，用于判断「当前处于正值区还是负值区」。 */
  virtualAngle = HINGE_SIGN * pitchDeg;

  /* 4) 不做静止锚定：它无法区分「漂移」与「真实静止姿态」，
        屏幕停在某角度时会把读数慢慢拉回零，反而破坏使用体验。
        零偏漂移由开机 3 秒静止校准 + 上面的加速度计修正负责。 */

  /* 5) 输出低通，进一步平滑（保持符号） */
  angleDeg += ANGLE_LPF_ALPHA * (virtualAngle - angleDeg);
}

/* ===========================================================================
 * 十二、按键（软件消抖 + 短按/长按）
 * =========================================================================== */

static void updateButton(uint32_t now) {
  static uint32_t lowSinceMs   = 0;
  static bool     bootChecked  = false;

  const bool raw = (digitalRead(PIN_BUTTON) == LOW);

  /* 防悬空保护：按键没接或另一端没接 GND 时，GPIO5 电平会乱跳，
     被状态机当成连续短按/长按，从而反复触发校准（实测 8 秒内触发 6 次短按）。
     判据：上电后持续低电平超过 2 秒即认为按键不可用，直接禁用按键功能；
     若之后检测到明确的释放（高电平），说明按键其实接好了，再启用。 */
  if (!bootChecked) {
    if (raw) {
      if (lowSinceMs == 0) {
        lowSinceMs = now;
      } else if (now - lowSinceMs > 2000) {
        if (buttonEnabled) {
          Serial.println(F("# 按键持续为低 2 秒：判定为未接/悬空，已禁用按键功能"));
        }
        buttonEnabled = false;
        bootChecked = true;
      }
    } else {
      lowSinceMs = 0;
      buttonEnabled = true;
      bootChecked = true;
    }
  } else if (!raw && !buttonEnabled) {
    buttonEnabled = true;
    Serial.println(F("# 检测到按键释放，重新启用按键功能"));
  }

  if (!buttonEnabled) {
    return;
  }

  switch (btnState) {
    case BTN_IDLE:
      if (raw) {
        btnEdgeMs = now;
        btnState  = BTN_DEBOUNCE_DOWN;
      }
      break;

    case BTN_DEBOUNCE_DOWN:
      if (!raw) {
        btnState = BTN_IDLE;                       // 抖动，忽略
      } else if (now - btnEdgeMs >= BTN_DEBOUNCE_MS) {
        btnPressedMs = now;
        btnState     = BTN_PRESSED;
      }
      break;

    case BTN_PRESSED:
      /* 长按立即触发重校准；btnLongLatched 保证「一次按下只触发一次」 */
      if (now - btnPressedMs >= BTN_LONG_MS && !btnLongLatched) {
        btnLongLatched = true;
        longPressCount++;
        startCalibration("long press");
        workMode = MODE_CALIBRATE;
        btnState = BTN_DEBOUNCE_UP;
        btnEdgeMs = now;
        oledCachedMode = -1;
      } else if (!raw) {
        btnState  = BTN_DEBOUNCE_UP;
        btnEdgeMs = now;
      }
      break;

    case BTN_DEBOUNCE_UP:
      if (raw) {
        btnState = BTN_PRESSED;                    // 抖动，回到按下态
      } else if (now - btnEdgeMs >= BTN_DEBOUNCE_MS) {
        const uint32_t held = btnEdgeMs - btnPressedMs;
        if (!btnLongLatched && held >= BTN_SHORT_MIN_MS && held < BTN_LONG_MS) {
          shortPressCount++;
          nextMode();
        }
        btnLongLatched = false;                    // 真正松手才解除闩锁
        btnState = BTN_IDLE;
      }
      break;

    default:
      btnState = BTN_IDLE;
      break;
  }
}

/** 短按切模式：default -> calibrate -> debug -> default */
static void nextMode() {
  switch (workMode) {
    case MODE_DEFAULT:
      workMode = MODE_CALIBRATE;
      startCalibration("short press enter calibrate mode");
      break;
    case MODE_CALIBRATE:
      workMode = MODE_DEBUG;
      break;
    case MODE_DEBUG:
    default:
      workMode = MODE_DEFAULT;
      break;
  }
  oledCachedMode = -1;
  Serial.print(F("# 模式切换到 "));
  Serial.println(modeText());
}

/* ===========================================================================
 * 十三、OLED 本地 UI（非阻塞，200ms 刷新）
 * =========================================================================== */

static void updateOled(uint32_t now, bool force) {
  if (!oledReady) {
    return;
  }
  if (!force && (now - lastOledMs < OLED_UPDATE_INTERVAL)) {
    return;
  }
  lastOledMs = now;

  if (calibActive) {
    const int pct = calibProgressPercent();
    if (!force && pct == oledCachedCalibPct) {
      return;
    }
    drawCalibrationScreen(now);
    return;
  }

  /* 主界面每 200ms 无条件重绘。
     这里刻意不做「数值没变就跳过」的优化：一旦 OLED 在 I2C 出错时静默丢帧，
     跳过逻辑会让屏幕一直停在上一次的旧数值上（现场表现为「角度不再更新」），
     而 128x64 全屏刷新在 400kHz 下只需约 25ms，代价可接受。 */
  drawMainScreen();
}

static void drawMainScreen() {
  const int barPct   = (int)((angleDeg / ANGLE_MAX_DEG) * 100.0f + 0.5f);
  /* 注意：显示必须用 lroundf 而不是 (int)(x + 0.5f)。
     (int) 是向零取整，对负数会吃掉符号：-0.7 + 0.5 = -0.2 -> (int) = 0，
     屏幕上就看不到负号了（曾因此误判"固件只输出正值"）。 */
  const int angleInt = (int)lroundf(angleDeg);

  oled.clearDisplay();
  oled.setTextSize(1);
  oled.setTextColor(SSD1306_WHITE);

  /* 第 1 行：标题 + 模式 */
  oled.setCursor(0, 0);
  oled.print(F("WinDuo "));
  oled.print(modeTextShort());

  /* 分隔线 */
  oled.drawFastHLine(0, 10, OLED_WIDTH, SSD1306_WHITE);

  /* 角度大字：最多显示 4 个字符位（含负号），超出按 4 位对齐，
     保证每帧占位一致、不出现残影。角度是无界累积值，可能超过 ±180。 */
  oled.setTextSize(3);
  oled.setCursor(0, 16);
  const int absAngle = (angleInt < 0) ? -angleInt : angleInt;
  if (absAngle < 10) {
    oled.print(F("  "));                 // 如 "-5"   -> 2 字符，补 2 空格
  } else if (absAngle < 100) {
    oled.print(F(" "));                  // 如 "-45"  -> 3 字符，补 1 空格
  }                                        // 如 "-180" -> 4 字符，不补
  oled.print(angleInt);
  oled.setTextSize(1);
  oled.setCursor(80, 24);
  oled.print(F("deg"));

  /* 进度条 */
  const int barY = 44, barH = 8;
  oled.drawRect(0, barY, OLED_WIDTH, barH, SSD1306_WHITE);
  const int fillW = (int)((float)(OLED_WIDTH - 4) * ((float)barPct / 100.0f));
  if (fillW > 0) {
    oled.fillRect(2, barY + 2, fillW, barH - 4, SSD1306_WHITE);
  }

  /* 状态行 */
  oled.setCursor(0, 55);
  oled.print(F("S:"));
  oled.print(statusText());
  oled.print(F("  "));
  oled.print(barPct);
  oled.print(F("%"));

  oled.display();

  oledCachedAngleInt = angleInt;
  oledCachedBarPct   = barPct;
  oledCachedMode     = (int)workMode;
  oledCachedStatus   = (int)sysStatus;
  oledCachedCalibPct = -1;
}

/**
 * OLED 健康监视：SSD1306 在 I2C 出错时会静默丢帧，屏幕就停在上一次的内容上，
 * 现场表现是「角度显示到某个值之后不再变化 / 跳回旧数值」。
 * 这里周期性探测 OLED 是否还在总线上应答，连续失败就重新初始化并强制整屏重绘。
 */
static void oledHealthCheck(uint32_t now) {
  static uint32_t lastCheckMs = 0;
  static uint8_t  failStreak  = 0;

  if (!oledReady) {
    return;
  }
  if (now - lastCheckMs < 2000) {
    return;
  }
  lastCheckMs = now;

  const bool alive = i2cPingQuiet(oledAddress);
  if (alive) {
    failStreak = 0;
    return;
  }

  failStreak++;
  Serial.print(F("# OLED 无应答 streak="));
  Serial.println(failStreak);
  if (failStreak < 2) {
    return;
  }
  failStreak = 0;

  /* 重新初始化；成功后强制下一帧整屏重绘，避免屏幕继续停在旧内容上 */
  if (initOled(oledAddress) || initOled(OLED_ADDR_MAIN) || initOled(OLED_ADDR_ALT)) {
    oledReady = true;
    oledCachedAngleInt = -1;
    oledCachedBarPct   = -1;
    oledCachedStatus   = -1;
    oledCachedMode     = -1;
    oledCachedCalibPct = -1;
    Wire.setClock(400000L);
    Serial.println(F("# OLED 已恢复"));
  } else {
    oledReady = false;
    Serial.println(F("# OLED 恢复失败，等待重新插入"));
  }
  refreshStatus();
}

static void drawCalibrationScreen(uint32_t now) {
  uint32_t elapsed = now - calibStartMs;
  if (elapsed > CALIB_WINDOW_MS) {
    elapsed = CALIB_WINDOW_MS;
  }
  const int pct = (int)((float)elapsed / (float)CALIB_WINDOW_MS * 100.0f);

  oled.clearDisplay();
  oled.setTextSize(1);
  oled.setTextColor(SSD1306_WHITE);
  oled.setCursor(0, 0);
  oled.println(F("Calibrating gyro..."));
  oled.setCursor(0, 12);
  oled.println(F("Keep device still"));

  oled.setTextSize(3);
  oled.setCursor(24, 28);
  oled.print(pct);
  oled.setTextSize(1);
  oled.setCursor(96, 36);
  oled.print(F("%"));

  oled.drawRect(0, 52, OLED_WIDTH, 8, SSD1306_WHITE);
  const int fillW = (int)((float)(OLED_WIDTH - 4) * ((float)pct / 100.0f));
  if (fillW > 0) {
    oled.fillRect(2, 54, fillW, 4, SSD1306_WHITE);
  }
  oled.display();

  oledCachedCalibPct = pct;
  oledCachedMode     = (int)workMode;
}

/* ===========================================================================
 * 十四、串口 JSON 输出
 * =========================================================================== */

static int calibProgressPercent() {
  if (!calibActive) {
    return 100;
  }
  uint32_t elapsed = millis() - calibStartMs;
  if (elapsed > CALIB_WINDOW_MS) {
    elapsed = CALIB_WINDOW_MS;
  }
  int pct = (int)((float)elapsed / (float)CALIB_WINDOW_MS * 100.0f);
  if (pct < 0)   pct = 0;
  if (pct > 100) pct = 100;
  return pct;
}

static void sendJsonLine() {
  JsonDocument doc;

  /* 角度必须写成「数值」并保留符号：先量化到 1 位小数再以整数除回来，
     既避免 45.19999 这样的浮点毛刺，也保证 PC 端 json.loads 拿到 float。
     注意：
       - 负数要向下取整方向补偿（-0.5），否则量化会偏向零；
       - 角度是无界累积值，先夹到 ±30000 度，避免 (int) 溢出（30 转/秒也追不上）。 */
  const float angleForJson = clampFloat(angleDeg, -30000.0f, 30000.0f);
  const int angleTenths = (int)(angleForJson * 10.0f + (angleForJson >= 0.0f ? 0.5f : -0.5f));
  doc["angle"]  = (float)(angleTenths / 10.0);
  doc["status"] = statusText();
  doc["mode"]   = modeText();
  doc["author"] = TXT_AUTHOR;

  if (workMode == MODE_DEBUG) {
    const int gyroCenti = (int)(gyroRateDps * 100.0f + (gyroRateDps >= 0 ? 0.5f : -0.5f));
    const int biasMilli = (int)(gyroBiasY * 1000.0f + (gyroBiasY >= 0 ? 0.5f : -0.5f));
    const int baseCenti = (int)(baselinePitchDeg * 100.0f + (baselinePitchDeg >= 0 ? 0.5f : -0.5f));
    /* pitch / virt 用于现场诊断符号问题：pitch 是当前绝对俯仰角，
       virt 是未经过低通的相对角（HINGE_SIGN 已应用），
       angle 是低通后的对外输出。三者同号即链路正常。 */
    const int pitchCenti = (int)(pitchDeg * 100.0f + (pitchDeg >= 0 ? 0.5f : -0.5f));
    const int virtCenti  = (int)(virtualAngle * 100.0f + (virtualAngle >= 0 ? 0.5f : -0.5f));
    doc["gyro"]  = (float)(gyroCenti / 100.0);
    doc["bias"]  = (float)(biasMilli / 1000.0);
    doc["base"]  = (float)(baseCenti / 100.0);
    doc["pitch"] = (float)(pitchCenti / 100.0);
    doc["virt"]  = (float)(virtCenti / 100.0);
    /* 修正链路诊断：dev=偏差，conf=倾角信度，corr=本帧修正量，frz=是否被冻结。
       若 corr 恒为 0 或 frz 恒为 1，说明加速度计修正根本没在工作。 */
    const int devMilli  = (int)(dbgDev * 1000.0f + (dbgDev >= 0 ? 0.5f : -0.5f));
    const int confMilli = (int)(dbgConf * 1000.0f + 0.5f);
    const int corrMilli = (int)(dbgCorr * 1000.0f + (dbgCorr >= 0 ? 0.5f : -0.5f));
    doc["dev"]  = (float)(devMilli / 1000.0);
    doc["conf"] = (float)(confMilli / 1000.0);
    doc["corr"] = (float)(corrMilli / 1000.0);
    doc["frz"]  = accelFrozen ? 1 : 0;
    /* 加速度计三轴读数（g）：用来判断哪个轴适合做倾角参考。
       基准姿态下读数接近 ±1g 的那个轴，就是当前的竖直轴。 */
    const int axM = (int)(accelG.x * 1000.0f + (accelG.x >= 0 ? 0.5f : -0.5f));
    const int ayM = (int)(accelG.y * 1000.0f + (accelG.y >= 0 ? 0.5f : -0.5f));
    const int azM = (int)(accelG.z * 1000.0f + (accelG.z >= 0 ? 0.5f : -0.5f));
    doc["ax"] = (float)(axM / 1000.0);
    doc["ay"] = (float)(ayM / 1000.0);
    doc["az"] = (float)(azM / 1000.0);
    doc["sp"]   = shortPressCount;
    doc["lp"]   = longPressCount;
  }
  if (calibActive) {
    doc["progress"] = calibProgressPercent();
  }

  serializeJson(doc, Serial);
  Serial.write('\n');
}

/* ===========================================================================
 * 十五、串口命令（主机 -> 设备，可选）
 * =========================================================================== */

static char    cmdBuffer[48];
static uint8_t cmdLen      = 0;
static bool    cmdOverflow = false;

static void pollSerialCommand(uint32_t now) {
  static uint32_t lastByteMs = 0;

  while (Serial.available() > 0) {
    const char ch = (char)Serial.read();
    lastByteMs = now;

    if (ch == '\n' || ch == '\r') {
      if (cmdLen > 0 && !cmdOverflow) {
        cmdBuffer[cmdLen] = '\0';
        handleCommand(cmdBuffer);
      }
      cmdLen = 0;
      cmdOverflow = false;
      continue;
    }
    if (cmdLen < sizeof(cmdBuffer) - 1) {
      cmdBuffer[cmdLen++] = ch;
    } else {
      cmdOverflow = true;         // 超长行丢弃，直到下一个换行
    }
  }

  if (cmdLen > 0 && (now - lastByteMs > 500)) {
    cmdLen = 0;
    cmdOverflow = false;
  }
}

/** 支持: cal / calibrate、mode=default|calibrate|debug、status */
static void handleCommand(const char *cmd) {
  String line(cmd);
  line.trim();
  line.toLowerCase();

  if (line == "cal" || line == "calibrate") {
    startCalibration("host command");
    return;
  }
  if (line == "status") {
    sendJsonLine();
    return;
  }
  if (line.startsWith("mode=")) {
    const String v = line.substring(5);
    if (v == "default") {
      workMode = MODE_DEFAULT;
    } else if (v == "calibrate" || v == "cal") {
      workMode = MODE_CALIBRATE;
    } else if (v == "debug") {
      workMode = MODE_DEBUG;
    } else {
      Serial.println(F("# 未知模式，可用 default|calibrate|debug"));
      return;
    }
    oledCachedMode = -1;
    Serial.print(F("# 模式切换到 "));
    Serial.println(modeText());
    return;
  }
  Serial.println(F("# 未知命令，可用 cal|mode=|status"));
}
