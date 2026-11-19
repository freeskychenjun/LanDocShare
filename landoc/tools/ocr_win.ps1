# Windows 系统 OCR（Windows.Media.Ocr）桥：把一批 PNG 交给系统识别，输出纯文本。
#
# 由 landoc/ocr.py 调用，不建议手敲。设计约束：
#   * 只用 Windows 自带的 OCR 引擎（随中文语言包安装），不下载、不安装任何东西；
#   * 纯 ASCII 源码：PowerShell 5.1 按 ANSI 读无 BOM 脚本，中文写在这里会乱码；
#   * 一次进程识别整批图（每张图一次 RecognizeAsync），避免每页起一次 PowerShell。
#
# 用法：
#   powershell -NoProfile -ExecutionPolicy Bypass -File ocr_win.ps1 -Mode langs
#   powershell -NoProfile -ExecutionPolicy Bypass -File ocr_win.ps1 -Mode batch ^
#       -JobFile job.json -OutDir <目录>
#
# batch 模式的 job.json：{"images":[{"id":"p0001","path":"C:\\...\\p0001.png"}]}
# 每张图的结果写到 <OutDir>\<id>.txt（UTF-8 无 BOM），行与行之间保留换行。

param(
    [ValidateSet('langs', 'batch')][string]$Mode = 'batch',
    [string]$JobFile = '',
    [string]$OutDir = '',
    [string]$Lang = 'zh-Hans-CN'
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime | Out-Null

# PowerShell 5.1 里 WinRT 类型必须带程序集限定名才解析得到
$OcrEngine = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$Language = [Windows.Globalization.Language, Windows.Foundation, ContentType = WindowsRuntime]
$StorageFile = [Windows.Storage.StorageFile, Windows.Foundation, ContentType = WindowsRuntime]
$FileAccessMode = [Windows.Storage.FileAccessMode, Windows.Foundation, ContentType = WindowsRuntime]
$RandomAccessStream = [Windows.Storage.Streams.IRandomAccessStream, Windows.Foundation, ContentType = WindowsRuntime]
$BitmapDecoder = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime]
$SoftwareBitmap = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Foundation, ContentType = WindowsRuntime]
$OcrResult = [Windows.Media.Ocr.OcrResult, Windows.Foundation, ContentType = WindowsRuntime]

# IAsyncOperation<T> 在 PowerShell 里不能直接 await，要靠 AsTask<T> 包一层再等。
$script:AsTask = $null
foreach ($method in [System.WindowsRuntimeSystemExtensions].GetMethods()) {
    if ($method.Name -ne 'AsTask' -or $method.GetParameters().Count -ne 1) { continue }
    $paramType = $method.GetParameters()[0].ParameterType
    if (-not $paramType.IsGenericType) { continue }
    if ($paramType.GetGenericTypeDefinition().FullName -eq 'Windows.Foundation.IAsyncOperation`1') {
        $script:AsTask = $method
        break
    }
}
if (-not $script:AsTask) { Write-Output 'error no AsTask'; exit 3 }

function Await($operation, [Type]$resultType) {
    $task = $script:AsTask.MakeGenericMethod($resultType).Invoke($null, @($operation))
    [void]$task.Wait(-1)
    return $task.Result
}

function Write-Utf8NoBom([string]$path, [string]$text) {
    [System.IO.File]::WriteAllText($path, $text, (New-Object System.Text.UTF8Encoding($false)))
}

if ($Mode -eq 'langs') {
    foreach ($language in $OcrEngine::AvailableRecognizerLanguages) {
        Write-Output $language.LanguageTag
    }
    exit 0
}

try {
    $job = Get-Content -LiteralPath $JobFile -Raw -Encoding UTF8 | ConvertFrom-Json
    $engine = $OcrEngine::TryCreateFromLanguage($Language::new($Lang))
    if ($null -eq $engine) { Write-Output ('error no engine for ' + $Lang); exit 4 }
    if (-not (Test-Path -LiteralPath $OutDir)) {
        New-Item -ItemType Directory -Path $OutDir -Force | Out-Null
    }
    $count = 0
    foreach ($image in $job.images) {
        $file = Await ($StorageFile::GetFileFromPathAsync([string]$image.path)) $StorageFile
        $stream = Await ($file.OpenAsync($FileAccessMode::Read)) $RandomAccessStream
        $text = ''
        try {
            $decoder = Await ($BitmapDecoder::CreateAsync($stream)) $BitmapDecoder
            $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) $SoftwareBitmap
            try {
                $result = Await ($engine.RecognizeAsync($bitmap)) $OcrResult
                foreach ($line in $result.Lines) {
                    $text = $text + $line.Text + "`n"
                }
            } finally {
                $bitmap.Dispose()
            }
        } finally {
            $stream.Dispose()
        }
        Write-Utf8NoBom (Join-Path $OutDir ([string]$image.id + '.txt')) $text
        $count++
    }
    Write-Output ('ok ' + $count)
    exit 0
} catch {
    Write-Output ('error ' + $_.Exception.Message)
    exit 1
}
