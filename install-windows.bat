@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

echo ========================================
echo   MemOS Windows 安装脚本
echo   Memory Operating System for AI Agents
echo ========================================
echo.

:: 检查 Python 是否安装
echo [1/6] 检查 Python 环境...
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Python，请先安装 Python 3.10 或更高版本
    echo 下载地址: https://www.python.org/downloads/
    pause
    exit /b 1
)

for /f "tokens=2" %%i in ('python --version 2^>^&1') do set PYTHON_VERSION=%%i
echo      Python 版本: !PYTHON_VERSION!

:: 检查 pip
echo.
echo [2/6] 检查 pip...
python -m pip --version >nul 2>&1
if errorlevel 1 (
    echo [错误] pip 未正确安装
    pause
    exit /b 1
)
echo      pip 已就绪

:: 检查是否在 MemOS 目录
set "PROJECT_DIR=%~dp0"
cd /d "%PROJECT_DIR%"
if not exist "pyproject.toml" (
    echo [错误] 未找到 pyproject.toml，请确保脚本位于 MemOS 根目录
    pause
    exit /b 1
)
echo      项目目录: %CD%

:: 创建虚拟环境
echo.
echo [3/6] 创建虚拟环境...
if exist ".venv" (
    echo      检测到已有虚拟环境，是否重新创建? (y/n)
    set /p RECREATE=
    if /i "!RECREATE!"=="y" (
        echo      删除旧虚拟环境...
        rmdir /s /q .venv 2>nul
    ) else (
        echo      使用现有虚拟环境
    )
)

if not exist ".venv" (
    echo      创建虚拟环境 .venv...
    python -m venv .venv
    if errorlevel 1 (
        echo [错误] 虚拟环境创建失败
        pause
        exit /b 1
    )
)
echo      虚拟环境就绪

:: 激活虚拟环境并安装依赖
echo.
echo [4/6] 安装依赖包 (可能需要几分钟)...
call .venv\Scripts\activate.bat

:: 升级 pip
echo      升级 pip...
python -m pip install --upgrade pip --quiet

:: 安装 poetry (如果需要)
python -m pip show poetry >nul 2>&1
if errorlevel 1 (
    echo      安装 Poetry...
    python -m pip install poetry --quiet
)

:: 安装项目依赖
echo      安装 MemOS 核心依赖...
pip install -e . --quiet
if errorlevel 1 (
    echo [错误] 核心依赖安装失败
    pause
    exit /b 1
)

:: 安装可选依赖 (根据需求)
echo.
echo [5/6] 配置可选依赖...
echo.
echo      请选择安装模式:
echo      1) 核心功能 (默认)
echo      2) 全部功能 (包含 torch, sentence-transformers 等大型包)
echo      3) 知识库功能 (tree-mem, mem-reader)
echo      4) 调度功能 (mem-scheduler)
echo      5) 向量搜索功能 (pref-mem)
echo.
set /p MODE="请输入选项 (1-5，默认为 1): "
if "!MODE!"=="" set MODE=1

if "!MODE!"=="2" (
    echo      安装全部功能...
    pip install -e ".[all]" --quiet
) else if "!MODE!"=="3" (
    echo      安装知识库功能...
    pip install -e ".[tree-mem,mem-reader]" --quiet
) else if "!MODE!"=="4" (
    echo      安装调度功能...
    pip install -e ".[mem-scheduler]" --quiet
) else if "!MODE!"=="5" (
    echo      安装向量搜索功能...
    pip install -e ".[pref-mem]" --quiet
) else (
    echo      使用核心功能
)

:: 创建 .env 文件
echo.
echo [6/6] 配置环境变量...
if not exist ".env" (
    if exist "docker\.env.example" (
        copy "docker\.env.example" ".env" >nul
        echo      已创建 .env 文件，请编辑以下内容:
        echo      - OPENAI_API_KEY
        echo      - MOS_EMBEDDER_API_KEY
        echo      - NEO4J_URI (如使用 tree-mem)
        echo      - QDRANT_HOST (如使用向量搜索)
    ) else (
        echo # MemOS Configuration > .env
        echo OPENAI_API_KEY=your_api_key_here >> .env
        echo MOS_EMBEDDER_API_KEY=your_embedder_key_here >> .env
        echo NEO4J_URI=bolt://localhost:7687 >> .env
        echo NEO4J_USER=neo4j >> .env
        echo NEO4J_PASSWORD=your_neo4j_password >> .env
        echo QDRANT_HOST=localhost >> .env
        echo QDRANT_PORT=6333 >> .env
        echo      已创建 .env 文件，请编辑配置
    )
) else (
    echo      .env 文件已存在
)

echo.
echo ========================================
echo   安装完成!
echo ========================================
echo.
echo 下一步:
echo.
if exist ".env" (
    echo 1. 编辑 .env 文件配置 API Key
)
echo 2. 启动服务:
echo.
echo    Docker 部署 ^(推荐^):
echo    ^    cd docker
echo    ^    docker compose up
echo.
echo    或本地部署:
echo    ^    需要先启动 Neo4j 和 Qdrant
echo    ^    .venv\Scripts\activate.bat
echo    ^    uvicorn memos.api.server_api:app --host 0.0.0.0 --port 8000 --workers 1
echo.
echo 3. 访问 http://localhost:8000/docs 查看 API 文档
echo.
pause
