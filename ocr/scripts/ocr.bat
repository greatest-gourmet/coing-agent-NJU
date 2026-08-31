@echo off
chcp 65001 >nul
REM ============================================
REM  local-ocr skill 便捷启动脚本（仓库便携版）
REM  调用同目录 ocr_tool.py（%~dp0 定位，可整体移动）
REM  用法:  ocr.bat <图片|PDF路径> [--json] [--engine auto|win|rapid|tess] [--preprocess]
REM  自测:  ocr.bat --selftest
REM ============================================
if "%~1"=="" (
    echo 用法: ocr.bat ^<图片或PDF路径^> [--json] [--engine auto^|win^|rapid^|tess] [--preprocess]
    echo 自测: ocr.bat --selftest
    exit /b 1
)
python "%~dp0ocr_tool.py" %*
