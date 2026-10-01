/**
 * 离线编译验证用桩库（stub）：Adafruit_Sensor.h
 * ---------------------------------------------------------------------------
 * 用途：本机无法联网安装 Adafruit Unified Sensor 库时，
 *       用最小但接口忠实的声明让 .ino 参与真实 xtensa-g++ 语法/类型检查。
 *       这些定义来自 Adafruit Unified Sensor 的公开接口（字段顺序与类型一致）。
 * 注意：桩库仅用于离线编译预检，不参与烧录，不发布到仓库。
 */
#ifndef _ADAFRUIT_SENSOR_STUB_H_
#define _ADAFRUIT_SENSOR_STUB_H_

#include <stdint.h>
#include <math.h>

#define SENSORS_GRAVITY_EARTH (9.80665F)

/** 传感器类型编号（与官方一致的关键取值） */
typedef enum {
  SENSOR_TYPE_ACCELEROMETER = (1),
  SENSOR_TYPE_MAGNETIC_FIELD = (2),
  SENSOR_TYPE_ORIENTATION = (3),
  SENSOR_TYPE_GYROSCOPE = (4),
  SENSOR_TYPE_LIGHT = (5),
  SENSOR_TYPE_PRESSURE = (6),
  SENSOR_TYPE_PROXIMITY = (8),
  SENSOR_TYPE_GRAVITY = (9),
  SENSOR_TYPE_LINEAR_ACCELERATION = (10),
  SENSOR_TYPE_ROTATION_VECTOR = (11),
  SENSOR_TYPE_RELATIVE_HUMIDITY = (12),
  SENSOR_TYPE_AMBIENT_TEMPERATURE = (13),
  SENSOR_TYPE_VOLTAGE = (15)
} sensors_type_t;

/** 统一的传感器事件结构 */
typedef struct {
  union {
    float v[3];
    struct {
      float x;
      float y;
      float z;
    };
    struct {
      float roll;
      float pitch;
      float heading;
    };
  };
  int32_t version;
  int32_t sensor_id;
  int32_t type;
  int32_t reserved0;
  int32_t timestamp;
  union {
    float data[4];
    struct {
      float x;
      float y;
      float z;
    } acceleration;
    struct {
      float x;
      float y;
      float z;
    } magnetic;
    struct {
      float x;
      float y;
      float z;
    } gyro;
    float temperature;
    float distance;
    float light;
    float pressure;
    float relative_humidity;
    float voltage;
    float orientation[3];
    float gravity[3];
    float linear_acceleration[3];
    float rotation_vector[4];
  };
} sensors_event_t;

/** 传感器能力描述结构 */
typedef struct {
  char name[12];
  int32_t version;
  int32_t sensor_id;
  int32_t type;
  float max_value;
  float min_value;
  float resolution;
  int32_t min_delay;
} sensor_t;

/** 所有 Adafruit 传感器驱动的基类 */
class Adafruit_Sensor {
public:
  virtual ~Adafruit_Sensor() {}
  virtual bool getEvent(sensors_event_t *) = 0;
  virtual void getSensor(sensor_t *) = 0;
};

#endif /* _ADAFRUIT_SENSOR_STUB_H_ */
