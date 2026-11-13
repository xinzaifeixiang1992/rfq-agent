<#
.SYNOPSIS
    RFQ 智能体 · 离线演示一键启动（断网可用，不依赖 MatrixOne / Memoria / LLM）。

.DESCRIPTION
    用 SQLite + Git4Data 仿真 + 规则引擎跑通完整链路：解析 -> 三档报价 -> 审批 MERGE/PICK
    -> 决策审计与重放 -> MemoryGate 拦截 -> 规则沙盒 -> 记忆回滚。

    环境变量在本进程内覆盖，不会修改 .env 里的生产配置（pydantic-settings 中
    环境变量优先级高于 .env 文件）。离线库使用 data/rfq_offline.db，与日常 rfq.db 隔离。

.PARAMETER Reset
    清空离线库并重新灌入合成数据与初始记忆。

.PARAMETER NoWarm
    跳过预热（默认会预先跑完 4 封样例，避免现场站在屏幕前等）。

.PARAMETER NoBrowser
    不自动打开浏览器。

.PARAMETER DryRun
    只检查环境并打印执行计划，不启任何服务、不改任何数据。

.PARAMETER Stop
    结束本脚本上次启动的两个进程。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\offline_demo.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\offline_demo.ps1 -Reset
#>
[CmdletBinding()]
param(
    [switch]$Reset,
    [switch]$NoWarm,
    [switch]$NoBrowser,
    [switch]$DryRun,
    [switch]$Stop,
    [int]$ApiPort = 8000,
    [int]$WebPort = 8501
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$PidFile = Join-Path $Root "data\offline_demo.pids"
Set-Location $Root

function Step($m) { Write-Host "[*] $m" -ForegroundColor Cyan }
function Ok($m) { Write-Host "    OK  $m" -ForegroundColor Green }
function Warn($m) { Write-Host "    !!  $m" -ForegroundColor Yellow }

# 每启动一个进程就记一次 PID：脚本中途报错也能用 -Stop 收尾
$script:StartedPids = @()
function Save-Pids {
    New-Item -ItemType Directory -Force -Path (Split-Path $PidFile) | Out-Null
    Set-Content -Path $PidFile -Value ($script:StartedPids -join ",") -Encoding ASCII
}

# ---------- -Stop：结束上次启动的进程 ----------
if ($Stop) {
    if (-not (Test-Path $PidFile)) { Warn "没有找到 $PidFile，无需结束"; return }
    foreach ($id in (Get-Content $PidFile)) {
        foreach ($one in ($id -split ",")) {
            if ($one -and $one.Trim() -match '^\d+$') {
                try { Stop-Process -Id ([int]$one) -Force -ErrorAction Stop; Ok "已结束 PID $one" }
                catch { Warn "PID $one 已退出或无权结束" }
            }
        }
    }
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    return
}

# ---------- 1. 解释器 ----------
Step "检查运行环境"
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    throw "找不到 $Python。请先执行：python -m venv .venv; .\.venv\Scripts\pip.exe install -r requirements.txt"
}
Ok "python = $Python"

# ---------- 2. 进程内覆盖 .env（不改文件） ----------
$env:DB_BACKEND = "sqlite"
$env:SQLITE_PATH = "data/rfq_offline.db"
$env:MEMORY_BACKEND = "git4data"      # SQLite 下由仿真层提供分支 / 快照 / DIFF，演示语义完整
$env:MEMORIA_API_KEY = ""
$env:LLM_BASE_URL = ""
$env:LLM_API_KEY = ""                 # 留空 -> 路由器降级 rule-engine，主流程照常跑通
$env:API_BASE = "http://127.0.0.1:$ApiPort"
$env:DEMO_MODE = "false"              # 展示完整页签，含管理入口之外的调试区
$env:ALLOW_ADMIN_RESET = "false"
$env:RATE_LIMIT_WRITES = "0"          # 预热连发 4 封，关闭写限流避免误伤
Ok "已覆盖为 sqlite + git4data(仿真) + rule-engine，未改动 .env"

# ---------- 3. 样例与离线库 ----------
$Samples = @(
    @{ f = "01_nordic_excel.eml"; label = "Excel 样例（合并单元格）"; after = "approve" },
    @{ f = "02_pacific_pdf.eml"; label = "PDF 样例（跨页表格）"; after = "leave" },
    @{ f = "03_rhein_body.eml"; label = "正文样例"; after = "leave" },
    @{ f = "04_sakura_poison.eml"; label = "投毒样例（MemoryGate）"; after = "leave" }
)
$NeedSamples = ($Samples | Where-Object { -not (Test-Path (Join-Path $Root "samples\$($_.f)")) }).Count -gt 0
$OfflineDb = Join-Path $Root "data\rfq_offline.db"

Step "准备演示数据"
if ($NeedSamples) { Ok "样例缺失，需要生成" } else { Ok "4 封样例已存在" }
if ($Reset) { Ok "将清空并重建离线库" }
elseif (-not (Test-Path $OfflineDb)) { Ok "离线库不存在，需要初始化" }
else { Ok "离线库已存在：data\rfq_offline.db" }

if ($DryRun) {
    Write-Host ""
    Write-Host "DryRun：以上检查通过，未启动任何服务。" -ForegroundColor Yellow
    if ($NeedSamples) { Write-Host "  将执行：python scripts\make_samples.py" }
    if ($Reset -or -not (Test-Path $OfflineDb)) { Write-Host "  将执行：python scripts\seed.py" }
    Write-Host "  将启动：uvicorn --port $ApiPort / streamlit --server.port $WebPort"
    Write-Host "  工作台地址：http://localhost:$WebPort"
    return
}

if ($NeedSamples) {
    & $Python scripts\make_samples.py
    if ($LASTEXITCODE -ne 0) { throw "生成样例失败" }
    Ok "样例已生成到 samples\"
}
if ($Reset -or -not (Test-Path $OfflineDb)) {
    & $Python scripts\seed.py
    if ($LASTEXITCODE -ne 0) { throw "初始化离线库失败" }
    Ok "离线库已初始化（20 客户 / 200 SKU / 2000 条历史 / 20 条规则 / 初始记忆）"
}

# ---------- 4. 启动 FastAPI ----------
Step "启动后端 FastAPI"
$ApiUrl = "http://127.0.0.1:$ApiPort"
$ApiUp = $false
try {
    $r = Invoke-WebRequest -Uri "$ApiUrl/health" -UseBasicParsing -TimeoutSec 2
    if ($r.StatusCode -eq 200) { $ApiUp = $true }
} catch { }
if ($ApiUp) {
    Warn "端口 $ApiPort 上已有服务在跑，直接复用（若不是本项目请先关闭）"
    $ApiProc = $null
} else {
    $ApiProc = Start-Process -FilePath $Python -PassThru -WorkingDirectory $Root `
        -ArgumentList "-m", "uvicorn", "app.api.main:app", "--host", "127.0.0.1", "--port", $ApiPort
    $ready = $false
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Milliseconds 500
        if ($ApiProc.HasExited) { throw "FastAPI 启动即退出，请手动运行 uvicorn 查看报错" }
        try {
            $r = Invoke-WebRequest -Uri "$ApiUrl/health" -UseBasicParsing -TimeoutSec 2
            if ($r.StatusCode -eq 200) { $ready = $true; break }
        } catch { }
    }
    if (-not $ready) { throw "FastAPI 30 秒内未就绪" }
    $script:StartedPids += $ApiProc.Id
    Save-Pids
    Ok "FastAPI 已就绪：$ApiUrl/health"
}

# ---------- 5. 预热：预先跑完 4 封样例 ----------
$WebProc = $null
if (-not $NoWarm) {
    Step "预热样例（离线走规则引擎，通常每封 2-5 秒）"
    $existing = 0
    # 注意：不能写 @(Invoke-RestMethod ...).Count —— 管道里整个 JSON 数组会被当成 1 个对象
    try { $rows = Invoke-RestMethod "$ApiUrl/rfq?limit=200" -TimeoutSec 30; $existing = @($rows).Count } catch { }
    $tmpJson = Join-Path $env:TEMP "rfq_warm_response.json"
    $tmpBody = Join-Path $env:TEMP "rfq_approve_body.json"
    if ($existing -ge $Samples.Count) {
        Ok "库中已有 $existing 单，跳过预热（要重建演示现场请加 -Reset）"
    }
    else {
        foreach ($s in $Samples) {
            $p = Join-Path $Root "samples\$($s.f)"
            if (-not (Test-Path $p)) { Warn "$($s.label)：找不到文件，跳过"; continue }
            # 响应先落盘再按 UTF-8 读：PowerShell 5.1 直接接收 curl 的 stdout 会被系统码页搞乱
            $code = & curl.exe -s -S -m 180 -o $tmpJson -w "%{http_code}" -X POST -F "eml=@$p" "$ApiUrl/rfq"
            $code = "$code".Trim()
            if ($LASTEXITCODE -ne 0 -or $code -ne "200") {
                Warn "$($s.label) 进单失败（HTTP $code）"
                if (Test-Path $tmpJson) { Warn (Get-Content -Raw -Encoding UTF8 $tmpJson).Trim() }
                continue
            }
            $new = Get-Content -Raw -Encoding UTF8 $tmpJson | ConvertFrom-Json
            Ok "$($s.label) 已进单 $($new.rfq_id)"
            # Excel 样例预先走完审批，让「决策审计 / 重放」页签一打开就有内容
            if ($s.after -eq "approve") {
                Set-Content -Path $tmpBody -Value '{"action":"approve","strategy":"balanced","approver":"demo"}' -Encoding ASCII
                $acode = & curl.exe -s -S -m 180 -o $tmpJson -w "%{http_code}" -X POST `
                    -H "Content-Type: application/json" -d "@$tmpBody" "$ApiUrl/rfq/$($new.rfq_id)/approve"
                $acode = "$acode".Trim()
                if ($LASTEXITCODE -eq 0 -and $acode -eq "200") {
                    $ap = Get-Content -Raw -Encoding UTF8 $tmpJson | ConvertFrom-Json
                    Ok "         已整单 MERGE，状态 = $($ap.record.status)"
                }
                else {
                    Warn "         审批预备失败（HTTP $acode），仍可在页面上手动批准"
                }
            }
        }
    }
}

# ---------- 6. 启动 Streamlit ----------
Step "启动前端工作台"
$WebProc = Start-Process -FilePath $Python -PassThru -WorkingDirectory $Root `
    -ArgumentList "-m", "streamlit", "run", "web\app.py", "--server.port", $WebPort, `
    "--server.headless", "true", "--browser.gatherUsageStats", "false"
$script:StartedPids += $WebProc.Id
Save-Pids
$okWeb = $false
for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Milliseconds 500
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$WebPort/_stcore/health" -UseBasicParsing -TimeoutSec 2
        if ($r.StatusCode -eq 200) { $okWeb = $true; break }
    } catch { }
}
if ($okWeb) { Ok "工作台已就绪：http://localhost:$WebPort" } else { Warn "工作台未响应，请检查 $WebPort 端口占用" }

# ---------- 7. PID 已在每个进程启动时记录，中途报错也能 -Stop 收尾 ----------

# ---------- 8. 演示顺序提示 ----------
Write-Host ""
Write-Host "================== 离线演示已就绪 ==================" -ForegroundColor Cyan
Write-Host "  工作台   http://localhost:$WebPort"
Write-Host "  后端     $ApiUrl/health"
Write-Host ""
Write-Host "  已预置的演示现场（四个页签都有戏，不用现场等）："
Write-Host "   [已提交] Excel 样例  -> ③ 决策审计：逐节点时间线 + 点「重放」看当时 vs 现在"
Write-Host "   [待审批] PDF 样例    -> ② 报价与审批：看物料匹配方法与分数、三档 DIFF，现场 MERGE / PICK"
Write-Host "   [待审批] 正文样例    -> 现场「驳回」，观察三个报价分支被丢弃"
Write-Host "   [待审批] 投毒样例    -> ⑤ 记忆网关：隐藏指令 / 零宽字符 / 虚假折扣 / Net180 全被拦"
Write-Host ""
Write-Host "  建议开场顺序（不要从上传开始，从算好的页面切入）："
Write-Host "   1. ② 报价与审批  选 PDF 样例 -> 讲匹配方法与分数 -> 讲三档 -> 改一行价 -> 批准（MERGE）"
Write-Host "   2. ③ 决策审计    选 Excel 样例 -> 时间线 -> 重放"
Write-Host "   3. ⑤ 记忆网关    选投毒样例 -> 看隔离队列 -> 批准部分 -> pre_merge 快照"
Write-Host "   4. ⑥ 规则沙盒    target_margin 改 0.26 -> 回测 -> DIFF -> 合并"
Write-Host "   5. ① 接收询价    （仅当对方要求看进件过程时才现场上传）"
Write-Host ""
Write-Host "  提醒：刚预置时重放的差异全为 0（数据没变）。想看到非零差异，"
Write-Host "        先在 ⑥ 规则沙盒把 target_margin 改成 0.26 并合并，再回 ③ 点重放。"
Write-Host ""
Write-Host "  结束服务： powershell -ExecutionPolicy Bypass -File scripts\offline_demo.ps1 -Stop"
Write-Host "  重置数据： powershell -ExecutionPolicy Bypass -File scripts\offline_demo.ps1 -Reset"
Write-Host "====================================================" -ForegroundColor Cyan

if (-not $NoBrowser) { Start-Process "http://localhost:$WebPort" }
