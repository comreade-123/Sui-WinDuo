/**
 * 离线编译验证用桩库（stub）：ArduinoJson.h（v7 风格 API 子集）
 * ---------------------------------------------------------------------------
 * 仅实现 .ino 用到的接口：JsonDocument、doc["k"] = 值、serializeJson(doc, Stream)。
 * 目的是让固件在真实编译器下做类型检查，同时能在 PC 侧把 JSON 输出打印出来
 * 做格式校验（键序 = 插入序，数值不加引号）。
 *
 * 注意：这是“接口形状”桩，不是 ArduinoJson 的实现：
 *   - 键仅存指针，值统一转成字符串保存；
 *   - 浮点按 setPrecision 指定的位数输出；
 *   - 字符串值加双引号，数值原样输出。
 */
#ifndef _ARDUINOJSON_STUB_H_
#define _ARDUINOJSON_STUB_H_

#include <Arduino.h>
#include <stdio.h>
#include <string.h>
#include <math.h>

/** 预序列化的字符串包装（真实 ArduinoJson 中用于避免再次转义） */
class SerializedValue {
public:
  SerializedValue() : raw_(false) {}
  explicit SerializedValue(const String &text) : text_(text), raw_(true) {}
  bool isRaw() const { return raw_; }
  const String &text() const { return text_; }
private:
  String text_;
  bool raw_;
};

inline SerializedValue serialized(const String &text) {
  return SerializedValue(text);
}

/** 文档对象：按键值对保存，序列化时按键插入顺序输出 */
class JsonDocument : public Print {
public:
  JsonDocument() : count_(0), precision_(9) {}

  /** 取/建键：返回自身，随后由 operator= 赋值 */
  JsonDocument &operator[](const char *key) {
    if (count_ < MAX_ENTRIES) {
      key_[count_] = key;
      value_[count_] = "";
      quoted_[count_] = false;
      count_++;
    }
    return *this;
  }

  /* ------- 赋值：字符串类（需要加引号） ------- */
  JsonDocument &operator=(const char *text) {
    store(String(text), true, 0);
    return *this;
  }
  JsonDocument &operator=(const String &text) {
    store(text, true, 0);
    return *this;
  }

  /** serialized(...) 的值按原样输出：数字/已格式化片段都不加引号 */
  JsonDocument &operator=(const SerializedValue &value) {
    store(value.text(), false, 0);
    return *this;
  }

  /* ------- 赋值：数值类（原样输出，不加引号） ------- */
  JsonDocument &operator=(bool value) {
    store(String(value ? 1 : 0), false, 0);
    return *this;
  }
  JsonDocument &operator=(int value) {
    store(String(value), false, 0);
    return *this;
  }
  JsonDocument &operator=(long value) {
    store(String(value), false, 0);
    return *this;
  }
  JsonDocument &operator=(unsigned int value) {
    store(String((unsigned long)value), false, 0);
    return *this;
  }
  JsonDocument &operator=(unsigned long value) {
    store(String(value), false, 0);
    return *this;
  }
  JsonDocument &operator=(float value) {
    store(formatFloat((double)value), false, 0);
    return *this;
  }
  JsonDocument &operator=(double value) {
    store(formatFloat(value), false, 0);
    return *this;
  }

  /** 有效数字位数（与 ArduinoJson 的 setPrecision 语义一致） */
  void setPrecision(unsigned int digits) {
    precision_ = (digits == 0 || digits > 17) ? 9 : digits;
  }
  unsigned int precision() const { return precision_; }

  int size() const { return count_; }
  const char *keyAt(int index) const { return key_[index]; }
  const String &valueAt(int index) const { return value_[index]; }
  bool quotedAt(int index) const { return quoted_[index]; }

  /** Print 接口：真实 ArduinoJson 中 serializeJson(doc, doc) 也合法，
      这里只需满足纯虚函数，序列化文本由 serializeTo 一次写出 */
  size_t write(uint8_t) override { return 1; }

  size_t serializeTo(Print &out) const {
    /* 先构造文本，再一次性写出：同时满足 serializeJson(doc, out) 与
       serializeJson(doc, doc) 两种用法 */
    char buffer[512];
    size_t used = 0;
    appendText(buffer, sizeof(buffer), used, "{");
    for (int i = 0; i < count_; i++) {
      if (i > 0) {
        appendText(buffer, sizeof(buffer), used, ",");
      }
      appendText(buffer, sizeof(buffer), used, "\"");
      appendText(buffer, sizeof(buffer), used, key_[i]);
      appendText(buffer, sizeof(buffer), used, "\":");
      if (quoted_[i]) {
        appendText(buffer, sizeof(buffer), used, "\"");
        appendText(buffer, sizeof(buffer), used, value_[i].c_str());
        appendText(buffer, sizeof(buffer), used, "\"");
      } else {
        appendText(buffer, sizeof(buffer), used, value_[i].c_str());
      }
    }
    appendText(buffer, sizeof(buffer), used, "}");
    out.write((const uint8_t *)buffer, used);
    return used;
  }

private:
  static const int MAX_ENTRIES = 12;

  static void appendText(char *buffer, size_t capacity, size_t &used, const char *text) {
    if (!text) {
      return;
    }
    while (*text != '\0' && used + 1 < capacity) {
      buffer[used++] = *text++;
    }
    buffer[used] = '\0';
  }

  void store(const String &text, bool quoted, int /*unused*/) {
    if (count_ > 0) {
      value_[count_ - 1] = text;
      quoted_[count_ - 1] = quoted;
    }
  }

  /** 用 snprintf 按有效数字位数格式化浮点，模拟 ArduinoJson 的浮点输出 */
  String formatFloat(double value) const {
    char buffer[48];
    const int decimals = (int)precision_ - 2;      // 有效数字 -> 小数位（近似）
    if (decimals < 0) {
      snprintf(buffer, sizeof(buffer), "%.0f", value);
    } else {
      snprintf(buffer, sizeof(buffer), "%.*f", decimals, value);
    }
    /* 去掉多余的尾随 0，但保留至少一个整数位 */
    String text(buffer);
    if (text.indexOf('.') >= 0) {
      while (text.length() > 0 && text.endsWith("0")) {
        text.remove(text.length() - 1);
      }
      if (text.endsWith(".")) {
        text.remove(text.length() - 1);
      }
    }
    return text;
  }

  const char *key_[MAX_ENTRIES];
  String value_[MAX_ENTRIES];
  bool quoted_[MAX_ENTRIES];
  int count_;
  unsigned int precision_;
};

inline size_t serializeJson(const JsonDocument &doc, Print &out) {
  return doc.serializeTo(out);
}

#endif /* _ARDUINOJSON_STUB_H_ */
