# 贡献指南（CONTRIBUTING）

感谢你愿意参与 **Sui-WinDuo**！本文档说明如何提交 Issue、修改代码与发起 Pull Request。

## 一、可以贡献什么

- 修复固件 bug（姿态解算、校准、按键时序、OLED 刷新等）
- 改进 PC 端解析与玻璃效果实现
- 补充文档、接线说明与验证步骤
- 提交**实物测试记录**（这是本项目目前最需要的东西，见 [docs/hardware_checklist.md](docs/hardware_checklist.md)）

## 二、开发环境

| 项目 | 要求 |
| --- | --- |
| 固件 | Arduino IDE 2.x + `esp32 by Espressif Systems` 开发板支持包 |
| 开发板 | ESP32-WROOM-32E（Maker-ESP32），备选 ESP32 Dev Module |
| 库 | Adafruit SSD1306、Adafruit GFX Library、MPU6050_tockn、ArduinoJson 7.x |
| PC 端 | Python 3.x + `pyserial`、`PyQt6`、`PyOpenGL`、`mss`、`Pillow`、`numpy` |

详细的安装步骤见 [README.md](README.md) 第 4 节。

## 三、目录约定（重要）

本仓库采用 **Arduino IDE 工程结构**：

- 固件唯一草图文件是 `firmware/WindowsDuo_EthanMaven/WindowsDuo_EthanMaven.ino`；
- Arduino IDE 要求 `.ino` 文件名与它所在的文件夹同名，因此**不要重命名该文件或改变它的目录层级**；
- 新增固件文件（`.h` / `.cpp`）请放在与草图相同的目录 `firmware/WindowsDuo_EthanMaven/` 下；
- PC 端 Python 代码放在 `pc/`，工具脚本放在 `tools/`，文档放在 `docs/`。

完整目录树见 [README.md](README.md) 第 8 节。

## 四、代码风格

固件（C++ / Arduino）：

- **中文注释、英文标识符**；
- **禁止使用 `delay()`**：所有定时都基于 `millis()` 的非阻塞调度；
- 常量集中放在文件顶部的配置区，不要散落在函数里；
- JSON 输出必须使用 **ArduinoJson** 序列化，不要手工拼接浮点字符串；
- 浮点输出统一保留固定小数位（例如 `angle` 保留 1 位）。

PC 端（Python）：

- 遵循 PEP 8，4 空格缩进；
- 串口解析要能容忍并**跳过以 `#` 开头的注释行**；
- 新增逻辑请补充 `pc/tests/test_pipeline.py` 中的测试用例。

文档（Markdown）：

- 中文撰写，**UTF-8 无 BOM** 编码；
- 表格每行的管道符数量必须一致；
- 不要写入未经验证的实测结论，未验证内容请放进「待实物验证」章节。

## 五、提交规范

推荐使用 Conventional Commits 风格的提交信息：

```text
feat: 新增按键长按触发重新校准
fix: 修正互补滤波在反方向时的角度符号
docs: 补充 Win10 玻璃效果降级说明
test: 增加 # 注释行解析用例
chore: 更新 .gitignore
```

一个提交只做一件事，提交信息用中文或英文均可，但请保持同类提交风格一致。

## 六、提交流程

```powershell
# 1. 同步主分支
git switch main
git pull --rebase

# 2. 新建分支（用 feat/ fix/ docs/ 前缀）
git switch -c feat/oled-refresh

# 3. 修改并自测后提交
git add .
git commit -m "feat: 优化 OLED 刷新内容缓存"

# 4. 推送并发起 PR
git push -u origin feat/oled-refresh
```

详细的 Git 与 GitHub 操作（含常见错误处理）见 [docs/git_guide.md](docs/git_guide.md)。

## 七、提交前自检

- [ ] 固件能在 Arduino IDE 中**无警告编译通过**（选择 Maker-ESP32 或 ESP32 Dev Module）；
- [ ] 已运行 `tools/verify_firmware.ps1` 静态检查（用法见 [tools/README_verify.md](tools/README_verify.md)）；
- [ ] 固件中**没有 `delay()`**；
- [ ] PC 端改动已运行 `pc/tests/test_pipeline.py`；
- [ ] 文档路径与实际文件一致，表格管道符数量对齐；
- [ ] 没有提交编译产物（`build/`、`*.bin`、`*.elf`、`*.map`、`*.hex`）、`__pycache__/` 与任何密钥文件（如 `arduino_secrets.h`）。

## 八、不要提交的内容

- 任何密码、Token、私钥、Wi-Fi 凭据（`arduino_secrets.h` 已在 [.gitignore](.gitignore) 中忽略）；
- 编译产物与本地缓存目录；
- 与本项目无关的大文件或二进制资源。

## 九、许可

本项目以 **MIT License** 发布（见 [LICENSE](LICENSE)）。你提交的代码默认以同样的 MIT 许可授权给本项目，
请确保你有权提交相关代码，且不包含来自其他项目的、许可不兼容的代码。

本项目是开源项目 [WindowsDuo](https://github.com/KaedeharaKazuha1029/WindowsDuo) 的二次创作，请一并尊重原项目的许可与署名。
