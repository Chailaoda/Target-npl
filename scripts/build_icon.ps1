Add-Type -AssemblyName System.Drawing

$size = 256
$bmp = New-Object System.Drawing.Bitmap($size, $size)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::HighQuality
$g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
$g.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
$g.Clear([System.Drawing.Color]::Transparent)

# 1. 绘制深蓝紫渐变圆角矩形
$rect = New-Object System.Drawing.Rectangle(12, 12, 232, 232)
$radius = 52
$path = New-Object System.Drawing.Drawing2D.GraphicsPath
$path.AddArc($rect.X, $rect.Y, $radius, $radius, 180, 90)
$path.AddArc($rect.Right - $radius, $rect.Y, $radius, $radius, 270, 90)
$path.AddArc($rect.Right - $radius, $rect.Bottom - $radius, $radius, $radius, 0, 90)
$path.AddArc($rect.X, $rect.Bottom - $radius, $radius, $radius, 90, 90)
$path.CloseFigure()

$pt1 = New-Object System.Drawing.Point(12, 12)
$pt2 = New-Object System.Drawing.Point(244, 244)
$c1 = [System.Drawing.Color]::FromArgb(255, 15, 23, 42)
$c2 = [System.Drawing.Color]::FromArgb(255, 30, 41, 59)
$brush = New-Object System.Drawing.Drawing2D.LinearGradientBrush($pt1, $pt2, $c1, $c2)
$g.FillPath($brush, $path)

# 青蓝外发光边框
$borderPen = New-Object System.Drawing.Pen([System.Drawing.Color]::FromArgb(220, 56, 189, 248), 5)
$g.DrawPath($borderPen, $path)

# 2. 绘制瞄准雷达圆环（Target）
$ring1Pen = New-Object System.Drawing.Pen([System.Drawing.Color]::FromArgb(120, 14, 165, 233), 4)
$g.DrawEllipse($ring1Pen, 46, 46, 164, 164)

$ring2Pen = New-Object System.Drawing.Pen([System.Drawing.Color]::FromArgb(240, 56, 189, 248), 5)
$ring2Pen.DashStyle = [System.Drawing.Drawing2D.DashStyle]::Dash
$g.DrawEllipse($ring2Pen, 72, 72, 112, 112)

$ring3Pen = New-Object System.Drawing.Pen([System.Drawing.Color]::FromArgb(255, 244, 63, 94), 6)
$g.DrawEllipse($ring3Pen, 98, 98, 60, 60)

# 3. 绘制准星十字线
$crossPen = New-Object System.Drawing.Pen([System.Drawing.Color]::FromArgb(250, 248, 250, 252), 4)
$g.DrawLine($crossPen, 128, 50, 128, 86)
$g.DrawLine($crossPen, 128, 170, 128, 206)
$g.DrawLine($crossPen, 50, 128, 86, 128)
$g.DrawLine($crossPen, 170, 128, 206, 128)

# 4. 靶心核心聚焦点
$dotBrush = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::White)
$g.FillEllipse($dotBrush, 120, 120, 16, 16)

$g.Flush()

# 转为 PNG 内存流
$ms = New-Object System.IO.MemoryStream
$bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
$pngBytes = $ms.ToArray()

$g.Dispose()
$bmp.Dispose()
$ms.Dispose()

# 封装为标准 Windows ICO
$fs = [System.IO.File]::Create("E:\workplace\Target-npl\app.ico")
$bw = New-Object System.IO.BinaryWriter($fs)

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
$bw.Write([UInt32]$pngBytes.Length) # Image data size
$bw.Write([UInt32]22) # Image offset (6 + 16 = 22)

# Image Data (PNG)
$bw.Write($pngBytes)
$bw.Flush()

$bw.Close()
$fs.Close()

Write-Host "ICO_GENERATED_SUCCESSFULLY"
