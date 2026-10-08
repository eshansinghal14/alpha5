@echo off
cd /d "C:\Users\eshan\Desktop\Coding Projects\alpha5"
"C:\Python312\python.exe" -m kalshi_infra.run_full_pipeline >> kalshi_infra\pipeline_log.txt 2>&1
echo BATCH_EXIT_CODE=%ERRORLEVEL% >> kalshi_infra\pipeline_log.txt
