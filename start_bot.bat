@echo off
chcp 65001 >nul
title Бот задач
rem Запуск Telegram-бота задач двойным щелчком.
rem Окно должно оставаться открытым, пока бот работает. Остановить бота: закрыть окно.

cd /d "%~dp0"

rem --- Ищем Python: сначала окружение .venv проекта, потом установленный в системе ---
set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY where py >nul 2>nul && set "PY=py -3"
if not defined PY goto no_python

rem --- Без файла настроек бот не запустится ---
if not exist ".env" if not exist ".env.txt" goto no_env

rem --- Если каких-то библиотек нет (первый запуск или после обновления), ставим их ---
%PY% -c "import aiogram, aiohttp_socks, socksio, supabase, openai, apscheduler, dotenv" >nul 2>nul
if errorlevel 1 (
    echo Устанавливаю библиотеки, это займёт минуту...
    %PY% -m pip install -r requirements.txt
    if errorlevel 1 goto pip_failed
)

:start
echo.
echo Запускаю бота. Не закрывайте это окно, пока бот нужен.
echo Не забудьте включить Happ.
echo.
%PY% -m bot
echo.
echo ------------------------------------------------------------
echo Бот остановился. Причина написана выше.
echo Исправьте её и нажмите любую клавишу, чтобы запустить снова,
echo или просто закройте окно.
echo ------------------------------------------------------------
pause >nul
goto start

:no_python
echo Не найден Python.
echo Установите Python 3.11 или новее с сайта python.org
echo и при установке отметьте галочку "Add python.exe to PATH".
pause
exit /b 1

:no_env
echo Не найден файл .env с настройками в папке:
echo %cd%
echo Создайте его по инструкции в README, шаг 8.
pause
exit /b 1

:pip_failed
echo Не удалось установить библиотеки. Проверьте интернет и попробуйте ещё раз.
pause
exit /b 1
