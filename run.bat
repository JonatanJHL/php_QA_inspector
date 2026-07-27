@echo off
title Iniciador de Orquestador QA PHP - Agentes Locales
echo ======================================================================
echo    INICIANDO EL ORQUESTADOR DE AGENTES QA PARA PHP (LOCAL Y GRATUITO)
echo ======================================================================
echo.

cd /d "%~dp0"

if exist .venv goto :activate
echo [INFO] Creando el entorno virtual de Python (.venv)...
python -m venv .venv
if errorlevel 1 goto :err_venv

:activate
echo [INFO] Activando entorno virtual e instalando dependencias...
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r backend\requirements.txt
if errorlevel 1 goto :err_deps

echo [INFO] Abriendo la aplicacion en tu navegador...
start http://127.0.0.1:8000

echo [INFO] Iniciando el servidor local uvicorn en puerto 8000 (disponible en tu red local)...
echo Presiona Ctrl+C para detener el servidor.
echo.
python -m uvicorn main:app --app-dir backend --host 0.0.0.0 --port 8000 --reload
goto :eof

:err_venv
echo [ERROR] No se pudo crear el entorno virtual de Python.
pause
exit /b 1

:err_deps
echo [ERROR] Error al instalar dependencias del backend.
pause
exit /b 1
