@echo off
chcp 65001 >nul
setlocal
set CLI=D:\ASUS\arduino-cli\arduino-cli.exe
set CFG=D:\ASUS\arduino-cli-env\arduino-cli.yaml
set SKETCH=D:\Deepseek-API-project\DSH\WinDuo-EthanMaven\firmware\WindowsDuo_EthanMaven
set BUILD=D:\ASUS\arduino-cli-env\build
set FQBN=esp32:3.3.1-cn:esp32
set PORT=%~1
if "%PORT%"=="" set PORT=COM3

echo ============================================
echo  WindowsDuo_EthanMaven  build + flash
echo  FQBN : %FQBN%
echo  PORT : %PORT%
echo ============================================

echo.
echo [1/2] Compiling...
"%CLI%" compile --fqbn %FQBN% --config-file "%CFG%" --build-path "%BUILD%" --export-binaries "%SKETCH%"
if errorlevel 1 ( echo COMPILE FAILED & exit /b 1 )

echo.
echo [2/2] Flashing to %PORT% ...
"D:\ASUS\Arduino\packages\esp32\tools\esptool_py\5.1.0-cn\esptool.exe" ^
  --chip esp32 --port %PORT% --baud 460800 --before default-reset --after hard-reset ^
  write-flash -z --flash-mode dio --flash-freq 80m --flash-size 4MB ^
  0x1000 "%BUILD%\WindowsDuo_EthanMaven.ino.bootloader.bin" ^
  0x8000 "%BUILD%\WindowsDuo_EthanMaven.ino.partitions.bin" ^
  0xe000 "D:\ASUS\Arduino\hardware\esp32\3.3.1-cn\tools\partitions\boot_app0.bin" ^
  0x10000 "%BUILD%\WindowsDuo_EthanMaven.ino.bin"
if errorlevel 1 ( echo FLASH FAILED & exit /b 1 )

echo.
echo ===== DONE. Opening serial monitor at 115200 (Ctrl+C to quit) =====
"%CLI%" monitor -p %PORT% -b 115200 --config-file "%CFG%"
endlocal
