# MemOS Windows 安装脚本
# Memory Operating System for AI Agents
#Requires -Version 5.1

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  MemOS Windows 安装脚本" -ForegroundColor Cyan
Write-Host "  Memory Operating System for AI Agents" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

$ErrorActionPreference = "Stop"

# 检查 Python
Write-Host "[1/7] 检查 Python 环境..." -ForegroundColor Yellow
try {
    $pythonVersion = python --version 2>&1
    Write-Host "      $pythonVersion" -ForegroundColor Green
} catch {
    Write-Host "[错误] 未找到 Python，请先安装 Python 3.10 或更高版本" -ForegroundColor Red
    Write-Host "      下载地址: https://www.python.org/downloads/" -ForegroundColor Red
    Read-Host "按 Enter 退出"
    exit 1
}

# 检查 pip
Write-Host "`n[2/7] 检查 pip..." -ForegroundColor Yellow
try {
    $pipVersion = python -m pip --version 2>&1
    Write-Host "      pip 已就绪" -ForegroundColor Green
} catch {
    Write-Host "[错误] pip 未正确安装" -ForegroundColor Red
    exit 1
}

# 切换到脚本目录
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectDir

if (-not (Test-Path "pyproject.toml")) {
    Write-Host "[错误] 未找到 pyproject.toml，请确保脚本位于 MemOS 根目录" -ForegroundColor Red
    exit 1
}
Write-Host "      项目目录: $PWD" -ForegroundColor Green

# 创建虚拟环境
Write-Host "`n[3/7] 创建虚拟环境..." -ForegroundColor Yellow
if (Test-Path ".venv") {
    $recreate = Read-Host "检测到已有虚拟环境，是否重新创建? (y/N)"
    if ($recreate -eq "y" -or $recreate -eq "Y") {
        Write-Host "      删除旧虚拟环境..." -ForegroundColor Gray
        Remove-Item -Recurse -Force ".venv"
    } else {
        Write-Host "      使用现有虚拟环境" -ForegroundColor Green
    }
}

if (-not (Test-Path ".venv")) {
    Write-Host "      创建虚拟环境 .venv..." -ForegroundColor Gray
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[错误] 虚拟环境创建失败" -ForegroundColor Red
        exit 1
    }
}
Write-Host "      虚拟环境就绪" -ForegroundColor Green

# 激活虚拟环境
Write-Host "`n[4/7] 激活虚拟环境并安装依赖..." -ForegroundColor Yellow
& ".venv\Scripts\Activate.ps1"

# 升级 pip
Write-Host "      升级 pip..." -ForegroundColor Gray
python -m pip install --upgrade pip --quiet

# 安装 poetry
Write-Host "      安装 Poetry..." -ForegroundColor Gray
python -m pip install poetry --quiet

# 安装核心依赖
Write-Host "      安装 MemOS 核心依赖..." -ForegroundColor Gray
pip install -e . --quiet
if ($LASTEXITCODE -ne 0) {
    Write-Host "[错误] 核心依赖安装失败" -ForegroundColor Red
    exit 1
}

# 选择安装模式
Write-Host "`n[5/7] 配置可选依赖..." -ForegroundColor Yellow
Write-Host ""
Write-Host "      请选择安装模式:" -ForegroundColor White
Write-Host "      1) 核心功能 (默认)" -ForegroundColor White
Write-Host "      2) 全部功能 (包含 torch, sentence-transformers 等大型包)" -ForegroundColor White
Write-Host "      3) 知识库功能 (tree-mem, mem-reader)" -ForegroundColor White
Write-Host "      4) 调度功能 (mem-scheduler)" -ForegroundColor White
Write-Host "      5) 向量搜索功能 (pref-mem)" -ForegroundColor White
Write-Host ""

$mode = Read-Host "请输入选项 (1-5，默认为 1)"
if ([string]::IsNullOrEmpty($mode)) { $mode = "1" }

switch ($mode) {
    "2" {
        Write-Host "      安装全部功能..." -ForegroundColor Gray
        pip install -e ".[all]" --quiet
    }
    "3" {
        Write-Host "      安装知识库功能..." -ForegroundColor Gray
        pip install -e ".[tree-mem,mem-reader]" --quiet
    }
    "4" {
        Write-Host "      安装调度功能..." -ForegroundColor Gray
        pip install -e ".[mem-scheduler]" --quiet
    }
    "5" {
        Write-Host "      安装向量搜索功能..." -ForegroundColor Gray
        pip install -e ".[pref-mem]" --quiet
    }
    default {
        Write-Host "      使用核心功能" -ForegroundColor Gray
    }
}

# 创建 .env 文件
Write-Host "`n[6/7] 配置环境变量..." -ForegroundColor Yellow
if (-not (Test-Path ".env")) {
    if (Test-Path "docker\.env.example") {
        Copy-Item "docker\.env.example" ".env"
        Write-Host "      已创建 .env 文件，请编辑以下内容:" -ForegroundColor Green
        Write-Host "      - OPENAI_API_KEY" -ForegroundColor Cyan
        Write-Host "      - MOS_EMBEDDER_API_KEY" -ForegroundColor Cyan
        Write-Host "      - NEO4J_URI (如使用 tree-mem)" -ForegroundColor Cyan
        Write-Host "      - QDRANT_HOST (如使用向量搜索)" -ForegroundColor Cyan
    } else {
        @"
# MemOS Configuration
OPENAI_API_KEY=your_api_key_here
MOS_EMBEDDER_API_KEY=your_embedder_key_here
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=your_neo4j_password
QDRANT_HOST=localhost
QDRANT_PORT=6333
"@ | Out-File -FilePath ".env" -Encoding UTF8
        Write-Host "      已创建 .env 文件，请编辑配置" -ForegroundColor Green
    }
} else {
    Write-Host "      .env 文件已存在" -ForegroundColor Green
}

# 验证安装
Write-Host "`n[7/7] 验证安装..." -ForegroundColor Yellow
try {
    $memosVersion = memos --version 2>&1
    Write-Host "      $memosVersion" -ForegroundColor Green
} catch {
    Write-Host "      memos CLI 已安装" -ForegroundColor Green
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  安装完成!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "下一步:" -ForegroundColor White
Write-Host ""
if (Test-Path ".env") {
    Write-Host "1. 编辑 .env 文件配置 API Key" -ForegroundColor Yellow
}
Write-Host "2. 启动服务:" -ForegroundColor Yellow
Write-Host ""
Write-Host "   Docker 部署 (推荐):" -ForegroundColor White
Write-Host "   cd docker" -ForegroundColor Gray
Write-Host "   docker compose up" -ForegroundColor Gray
Write-Host ""
Write-Host "   或本地部署:" -ForegroundColor White
Write-Host "   需要先启动 Neo4j 和 Qdrant" -ForegroundColor Gray
Write-Host "   .venv\Scripts\activate.bat" -ForegroundColor Gray
Write-Host "   uvicorn memos.api.server_api:app --host 0.0.0.0 --port 8000" -ForegroundColor Gray
Write-Host ""
Write-Host "3. 访问 http://localhost:8000/docs 查看 API 文档" -ForegroundColor Yellow
Write-Host ""

Read-Host "按 Enter 退出"
