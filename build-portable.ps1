# 译页 Yiye Paper 便携版打包脚本
# 用法: .\build-portable.ps1
# 产出: yiye-portable.zip(解压后双击 启动.bat 即可使用)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$OutDir = Join-Path $Root "dist"
$ZipName = "yiye-portable.zip"
$Stage = Join-Path $OutDir "yiye-portable"

Write-Host "=== 译页 Yiye Paper 便携版打包 ===" -ForegroundColor Cyan

# 清理旧产物
if (Test-Path $Stage) { Remove-Item $Stage -Recurse -Force }
New-Item -ItemType Directory -Path $Stage -Force | Out-Null

# 复制应用文件(排除开发/测试/运行时数据)
$include = @(
    "app", "docs",
    "server.mjs", "engine_worker.py",
    "package.json", "pyproject.toml", "uv.lock",
    "启动.bat",
    "data\glossaries"
)
foreach ($item in $include) {
    $src = Join-Path $Root $item
    if (Test-Path $src) {
        $dst = Join-Path $Stage $item
        $parent = Split-Path $dst -Parent
        if (-not (Test-Path $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
        Copy-Item $src $dst -Recurse -Force
        Write-Host "  + $item"
    }
}

# 写便携版说明
@"
# 译页 Yiye Paper 便携版

## 首次使用
1. 确保已安装 Node.js (https://nodejs.org) 和 uv (https://docs.astral.sh/uv/)
2. 双击 启动.bat
3. 浏览器自动打开 http://127.0.0.1:4173

## 系统要求
- Windows 10/11
- Node.js 18+
- uv(Python 包管理器)
- 至少 8 GB 内存(运行本地大模型需要更多)

## 翻译模型
- 本地:安装 Ollama (https://ollama.com) 后拉取模型,推荐 qwen2.5:7b
- 云端:在设置中配置 OpenAI-compatible API 的地址、模型和 key
- 详见 docs/翻译服务接入指南.md
"@ | Set-Content (Join-Path $Stage "README-便携版.md") -Encoding UTF8

# 打包 zip
$zipPath = Join-Path $OutDir $ZipName
if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
Compress-Archive -Path $Stage -DestinationPath $zipPath -Force
$size = [math]::Round((Get-Item $zipPath).Length / 1MB, 1)
Write-Host "`n打包完成: $zipPath ($size MB)" -ForegroundColor Green
