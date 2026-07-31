# V1.1.4 归档复制脚本
# 在桌面打开 PowerShell 运行此脚本

$src = "C:\Users\indigo\Desktop\dingtalk-stream-bot"
$dst = "C:\Users\indigo\Desktop\ai与钉钉连接器\V1.1.4"

# 创建目录
New-Item -ItemType Directory -Force -Path "$dst\handlers" | Out-Null
New-Item -ItemType Directory -Force -Path "$dst\utils" | Out-Null

# 复制根目录文件
Copy-Item "$src\main.py" "$dst\"
Copy-Item "$src\CHANGELOG.md" "$dst\"
Copy-Item "$src\config.yaml" "$dst\"
Copy-Item "$src\requirements.txt" "$dst\"
Copy-Item "$src\build.py" "$dst\"

# 复制 handlers 目录
Copy-Item "$src\handlers\*.py" "$dst\handlers\" -Force

# 复制 utils 目录
Copy-Item "$src\utils\*.py" "$dst\utils\" -Force

# 复制 build 目录（如有）
if (Test-Path "$src\build") {
    Copy-Item "$src\build" "$dst\" -Recurse -Force
}

Write-Host "✅ V1.1.4 归档复制完成！" -ForegroundColor Green
Get-ChildItem $dst
