Add-Type -AssemblyName System.Drawing

$size = 256
$bmp = New-Object System.Drawing.Bitmap $size, $size
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::HighQuality
$g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
$g.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
$g.Clear([System.Drawing.Color]::Transparent)

# 1. 绘制圆角矩形背景（科技深蓝渐变）
$rect = New-Object System.Drawing.Rectangle 14, 14, 228, 228
$radius = 52
$path = New-Object System.Drawing.Drawing2D.GraphicsPath
$path.AddArc($rect.X, $rect.Y, $radius, $radius, 180, 90)
$path.AddArc($rect.Right - $radius, $rect.Y, $radius, $radius, 270, 90)
$path.AddArc($rect.Right - $radius, $rect.Bottom - $radius, $radius, $radius, 0, 90)
$path.AddArc($rect.X, $rect.Bottom - $radius, $radius, $radius, 90, 90)
$path.CloseFigure()

$pt1 = New-Object System.Drawing.Point 14, 14
$pt2 = New-Object System.Drawing.Point 242, 242
$c1 = [System.Drawing.Color]::FromArgb(255, 15, 23, 42)
$c2 = [System.Drawing.Color]::FromArgb(255, 30, 41, 59)
$brush = New-Object System.Drawing.Drawing2D.LinearGradientBrush $pt1, $pt2, $c1, $c2
$g.FillPath($brush, $path)

# 外边框发光线
$penBorderColor = [System.Drawing.Color]::FromArgb(200, 56, 189, 248)
$penBorder = New-Object System.Drawing.Pen $penBorderColor, 4
$g.DrawPath($penBorder, $path)

# 2. 绘制靶心精准定位雷达圆环（Target / 筛选准星）
$penRing1Color = [System.Drawing.Color]::FromArgb(100, 14, 165, 233)
$penRing1 = New-Object System.Drawing.Pen $penRing1Color, 4
$g.DrawEllipse($penRing1, 48, 48, 160, 160)

$penRing2Color = [System.Drawing.Color]::FromArgb(220, 56, 189, 248)
$penRing2 = New-Object System.Drawing.Pen $penRing2Color, 5
$penRing2.DashStyle = [System.Drawing.Drawing2D.DashStyle]::Dash
$g.DrawEllipse($penRing2, 74, 74, 108, 108)

$penRing3Color = [System.Drawing.Color]::FromArgb(255, 244, 63, 94)
$penRing3 = New-Object System.Drawing.Pen $penRing3Color, 6
$g.DrawEllipse($penRing3, 98, 98, 60, 60)

# 3. 绘制十字准心瞄准线
$penCrossColor = [System.Drawing.Color]::FromArgb(240, 248, 250, 252)
$penCross = New-Object System.Drawing.Pen $penCrossColor, 4
$g.DrawLine($penCross, 128, 54, 128, 88)
$g.DrawLine($penCross, 128, 168, 128, 202)
$g.DrawLine($penCross, 54, 128, 88, 128)
$g.DrawLine($penCross, 168, 128, 202, 128)

# 4. 靶心核心亮点（代表目标匹配）
$coreColor = [System.Drawing.Color]::FromArgb(255, 255, 255, 255)
$coreBrush = New-Object System.Drawing.SolidBrush $coreColor
$g.FillEllipse($coreBrush, 120, 120, 16, 16)

$g.Flush()

# 保存为 PNG 内存流
$ms = New-Object System.IO.MemoryStream
$bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
$pngBytes = $ms.ToArray()

$g.Dispose()
$bmp.Dispose()
$ms.Dispose()

# 构造标准 ICO 文件并写入
$icoStream = New-Object System.IO.MemoryStream
$bw = New-Object System.IO.BinaryWriter $icoStream

# ICONDIR
$bw.Write([UInt16]0) # Reserved
$bw.Write([UInt16]1) # Type 1 = ICO
$bw.Write([UInt16]1) # Count = 1

# ICONDIRENTRY
$bw.Write([Byte]0)    # Width 256 -> 0
$bw.Write([Byte]0)    # Height 256 -> 0
$bw.Write([Byte]0)    # Palette
$bw.Write([Byte]0)    # Reserved
$bw.Write([UInt16]1)  # Color planes
$bw.Write([UInt16]32) # Bits per pixel
$bw.Write([UInt32]$pngBytes.Length) # Image bytes
$bw.Write([UInt32]22) # Image offset (6 + 16 = 22)

# Image Data
$bw.Write($pngBytes)
$bw.Flush()

$targetPath = "E:\workplace\Target-npl\app.ico"
[System.IO.File]::WriteAllBytes($targetPath, $icoStream.ToArray())
$bw.Dispose()
$icoStream.Dispose()

Write-Host "ICON_SUCCESS: $targetPath"
