@echo off
rem build.bat: Windows batch script to mux video and audio into demo.mp4
setlocal enabledelayedexpansion

set SCRIPT_DIR=%~dp0
cd /d "%SCRIPT_DIR%"

echo ==========================================================
echo  Project Venessa - Video Assembly Pipeline (Windows)
echo ==========================================================

rem Check ffmpeg
where ffmpeg >nul 2>nul
if %errorlevel% neq 0 (
    echo Error: ffmpeg is not found on PATH.
    exit /b 1
)

rem Check python
where python >nul 2>nul
if %errorlevel% neq 0 (
    echo Error: python is not found on PATH.
    exit /b 1
)

echo.
echo ^>^> Step 1: Aligning audio and generating captions...
python build_assets.py
if %errorlevel% neq 0 (
    echo Error during build_assets.py execution.
    exit /b %errorlevel%
)

rem Find latest webm file in recordings
set LATEST_VIDEO=
for /f "delims=" %%I in ('dir /b /a-d /o-d "%SCRIPT_DIR%recordings\*.webm" 2^>nul') do (
    set LATEST_VIDEO=%SCRIPT_DIR%recordings\%%I
    goto :found_video
)

:found_video
if "%LATEST_VIDEO%"=="" (
    echo Error: No .webm recordings found in %SCRIPT_DIR%recordings. Run record.js first.
    exit /b 1
)

echo ^>^> Found recorded video: %LATEST_VIDEO%

echo.
echo ^>^> Step 2: Muxing video and audio into demo.mp4...
ffmpeg -y -i "%LATEST_VIDEO%" -i "%SCRIPT_DIR%full_narration.wav" -c:v libx264 -pix_fmt yuv420p -preset medium -crf 20 -c:a aac -b:a 192k -shortest "%SCRIPT_DIR%demo.mp4"
if %errorlevel% neq 0 (
    echo Error creating demo.mp4.
    exit /b %errorlevel%
)

echo ^>^> Successfully built: %SCRIPT_DIR%demo.mp4

echo.
echo ^>^> Step 3: Burning subtitles into demo_with_captions.mp4...
ffmpeg -y -i "%SCRIPT_DIR%demo.mp4" -vf "subtitles=captions.srt:force_style='FontSize=18,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BackColour=&H80000000,BorderStyle=4,MarginV=30'" -c:a copy "%SCRIPT_DIR%demo_with_captions.mp4"
if %errorlevel% neq 0 (
    echo Warning: Subtitle burn-in skipped or failed. demo.mp4 is ready!
) else (
    echo ^>^> Successfully built: %SCRIPT_DIR%demo_with_captions.mp4
)

echo.
echo ==========================================================
echo  Pipeline Complete!
echo  Outputs:
echo    - demo.mp4 (Clean video + narration)
echo    - demo_with_captions.mp4 (Burned-in captions)
echo    - captions.srt (SubRip subtitles)
echo ==========================================================
pause
