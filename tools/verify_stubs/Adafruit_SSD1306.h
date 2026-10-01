/**
 * 离线编译验证用桩库（stub）：Adafruit_SSD1306.h
 * ---------------------------------------------------------------------------
 * 接口与 Adafruit SSD1306 v2.5.x 一致（含 SSD1306_SWITCHCAPVCC 与地址参数形式）。
 */
#ifndef _ADAFRUIT_SSD1306_STUB_H_
#define _ADAFRUIT_SSD1306_STUB_H_

#include <Adafruit_GFX.h>
#include <Wire.h>

#define SSD1306_I2C_ADDRESS 0x3C
#define SSD1306_SWITCHCAPVCC 0x02
#define SSD1306_EXTERNALVCC 0x01
#define SSD1306_BLACK 0
#define SSD1306_WHITE 1
#define SSD1306_INVERSE 2

#define SSD1306_MEMORYMODE 0x20
#define SSD1306_COLUMNADDR 0x21
#define SSD1306_PAGEADDR 0x22

class Adafruit_SSD1306 : public Adafruit_GFX {
public:
  Adafruit_SSD1306(uint8_t w, uint8_t h, TwoWire *twi = &Wire,
                   int8_t rst_pin = -1, uint32_t clkDuring = 400000UL,
                   uint32_t clkAfter = 100000UL);
  ~Adafruit_SSD1306();

  bool begin(uint8_t switchvcc = SSD1306_SWITCHCAPVCC, uint8_t i2caddr = 0,
             bool reset = true, bool periphBegin = true);
  void display(void);
  void clearDisplay(void);
  void dim(bool dim);
  void invertDisplay(bool i);
  void startscrollright(uint8_t start, uint8_t stop);
  void stopscroll(void);
  void ssd1306_command(uint8_t c);
  bool getPixel(int16_t x, int16_t y);

  /* 实现基类纯虚函数，使本类可实例化（官方库由 SSPI/I2C 派生类实现） */
  void drawPixel(int16_t x, int16_t y, uint16_t color) override;

protected:
  int8_t _rst;
  uint8_t _vccstate;
  uint8_t _i2caddr;
};

#endif /* _ADAFRUIT_SSD1306_STUB_H_ */
