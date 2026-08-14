@echo off
cd /d "C:\Users\eshan\Desktop\Coding Projects\alpha5\kalshi_infra"
"C:\Python312\python.exe" run_full_pipeline.py >> pipeline_log.txt 2>&1
echo BATCH_EXIT_CODE=%ERRORLEVEL% >> pipeline_log.txt
