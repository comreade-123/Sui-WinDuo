/**
 * 离线编译验证用桩库（stub）：Adafruit_GFX.h
 * ---------------------------------------------------------------------------
 * 仅保留 .ino 实际调用的绘图接口，方法签名与 Adafruit GFX Library 一致。
 */
#ifndef _ADAFRUIT_GFX_STUB_H_
#define _ADAFRUIT_GFX_STUB_H_

#include <Arduino.h>

#define SSD1306_BLACK 0
#define SSD1306_WHITE 1
#define SSD1306_INVERSE 2

#ifndef BLACK
#define BLACK 0x0000
#endif
#ifndef WHITE
#define WHITE 0xFFFF
#endif

/// 基础字体指针类型（真实库中为 GFXfont*）
class GFXfont;

class Adafruit_GFX : public Print {
public:
  Adafruit_GFX(int16_t w, int16_t h);
  virtual ~Adafruit_GFX();

  virtual void drawPixel(int16_t x, int16_t y, uint16_t color) = 0;

  virtual void startWrite(void);
  virtual void writePixel(int16_t x, int16_t y, uint16_t color);
  virtual void writeFillRect(int16_t x, int16_t y, int16_t w, int16_t h, uint16_t color);
  virtual void writeFastVLine(int16_t x, int16_t y, int16_t h, uint16_t color);
  virtual void writeFastHLine(int16_t x, int16_t y, int16_t w, uint16_t color);
  virtual void writeLine(int16_t x0, int16_t y0, int16_t x1, int16_t y1, uint16_t color);
  virtual void endWrite(void);

  void drawFastVLine(int16_t x, int16_t y, int16_t h, uint16_t color);
  void drawFastHLine(int16_t x, int16_t y, int16_t w, uint16_t color);
  void drawLine(int16_t x0, int16_t y0, int16_t x1, int16_t y1, uint16_t color);
  void drawRect(int16_t x, int16_t y, int16_t w, int16_t h, uint16_t color);
  void fillRect(int16_t x, int16_t y, int16_t w, int16_t h, uint16_t color);
  void fillScreen(uint16_t color);
  void drawCircle(int16_t x0, int16_t y0, int16_t r, uint16_t color);
  void fillCircle(int16_t x0, int16_t y0, int16_t r, uint16_t color);
  void drawRoundRect(int16_t x, int16_t y, int16_t w, int16_t h, int16_t r, uint16_t color);
  void fillRoundRect(int16_t x, int16_t y, int16_t w, int16_t h, int16_t r, uint16_t color);
  void drawBitmap(int16_t x, int16_t y, const uint8_t bitmap[], int16_t w, int16_t h, uint16_t color);

  void setCursor(int16_t x, int16_t y);
  void setTextColor(uint16_t c);
  void setTextColor(uint16_t c, uint16_t bg);
  void setTextSize(uint8_t s);
  void setTextWrap(bool w);
  void setRotation(uint8_t r);
  void cp437(bool x = true);

  int16_t width(void) const { return WIDTH; }
  int16_t height(void) const { return HEIGHT; }
  uint8_t getRotation(void) const { return rotation; }
  int16_t getCursorX(void) const { return cursor_x; }
  int16_t getCursorY(void) const { return cursor_y; }

  size_t write(uint8_t) override;
  size_t write(const uint8_t *buffer, size_t size) override;

protected:
  int16_t WIDTH;
  int16_t HEIGHT;
  int16_t _width;
  int16_t _height;
  int16_t cursor_x;
  int16_t cursor_y;
  uint8_t textsize_x;
  uint8_t textsize_y;
  uint16_t textcolor;
  uint16_t textbgcolor;
  bool wrap;
  uint8_t rotation;
  bool _cp437;
};

#endif /* _ADAFRUIT_GFX_STUB_H_ */
