/**
 * 离线编译验证用桩库（stub）：Adafruit_MPU6050.h
 * ---------------------------------------------------------------------------
 * 接口与 Adafruit MPU6050 v2.2.x 公开头文件保持一致（枚举名、方法名、返回类型）。
 * 用于在无网络环境下对 .ino 做真实编译器的语法/类型检查。
 */
#ifndef _ADAFRUIT_MPU6050_STUB_H_
#define _ADAFRUIT_MPU6050_STUB_H_

#include <Adafruit_Sensor.h>
#include <Wire.h>

/** 加速度量程 */
typedef enum {
  MPU6050_RANGE_2_G = 0b00,
  MPU6050_RANGE_4_G = 0b01,
  MPU6050_RANGE_8_G = 0b10,
  MPU6050_RANGE_16_G = 0b11
} mpu6050_accel_range_t;

/** 陀螺仪量程 */
typedef enum {
  MPU6050_RANGE_250_DEG = 0b00,
  MPU6050_RANGE_500_DEG = 0b01,
  MPU6050_RANGE_1000_DEG = 0b10,
  MPU6050_RANGE_2000_DEG = 0b11
} mpu6050_gyro_range_t;

/** 数字低通滤波带宽 */
typedef enum {
  MPU6050_BAND_260_HZ = 0b000,
  MPU6050_BAND_184_HZ = 0b001,
  MPU6050_BAND_94_HZ = 0b010,
  MPU6050_BAND_44_HZ = 0b011,
  MPU6050_BAND_21_HZ = 0b100,
  MPU6050_BAND_10_HZ = 0b101,
  MPU6050_BAND_5_HZ = 0b110
} mpu6050_bandwidth_t;

/** 时钟源 */
typedef enum {
  MPU6050_CLOCK_INTERNAL = 0x00,
  MPU6050_CLOCK_PLL_XGYRO = 0x01
} mpu6050_clock_source_t;

class Adafruit_MPU6050 : public Adafruit_Sensor {
public:
  Adafruit_MPU6050();
  ~Adafruit_MPU6050();

  bool begin(uint8_t i2c_addr = 0x68, TwoWire *wire = &Wire);
  bool getEvent(sensors_event_t *accel, sensors_event_t *gyro, sensors_event_t *temp);
  void getSensor(sensor_t *accel, sensor_t *gyro, sensor_t *temp);

  /* 覆盖基类纯虚函数，保持 Adafruit_Sensor 接口契约（与官方实现一致） */
  bool getEvent(sensors_event_t *event) override;
  void getSensor(sensor_t *sensor) override;

  void setAccelerometerRange(mpu6050_accel_range_t);
  mpu6050_accel_range_t getAccelerometerRange(void);
  void setGyroRange(mpu6050_gyro_range_t);
  mpu6050_gyro_range_t getGyroRange(void);
  void setFilterBandwidth(mpu6050_bandwidth_t bandwidth);
  mpu6050_bandwidth_t getFilterBandwidth(void);
  void setClockSource(mpu6050_clock_source_t clock);
  void setHighPassFilterEnabled(bool enable);
  void setHighPassFilterCutoff(mpu6050_bandwidth_t bandwidth);
  void reset(void);
  void sleep(bool enable);
  float getTemperature(void);
};

#endif /* _ADAFRUIT_MPU6050_STUB_H_ */
