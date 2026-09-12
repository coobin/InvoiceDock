from __future__ import annotations

import random
import secrets
import time

from fastapi import Request

# 排除容易混淆的字符：0, O, 1, I, l
CAPTCHA_CHARS = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
CAPTCHA_EXPIRY_SECONDS = 300  # 5 分钟有效


def generate_captcha_text(length: int = 4) -> str:
    """生成指定长度的随机验证码文本。"""
    return "".join(secrets.choice(CAPTCHA_CHARS) for _ in range(length))


def generate_captcha_svg(text: str, width: int = 120, height: int = 40) -> str:
    """生成带有干扰线、噪点及字符随机旋转的轻量级 SVG 验证码图像。

    纯本地动态生成，零外部字体或第三方服务依赖，各操作系统与移动端原生高清晰度渲染。
    """
    # 随机柔和背景色
    bg_r = random.randint(238, 248)
    bg_g = random.randint(240, 250)
    bg_b = random.randint(242, 252)

    svg_parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" style="background-color: rgb({bg_r},{bg_g},{bg_b}); border-radius: 6px; user-select: none; cursor: pointer;">'
    ]

    # 绘制 4~6 条随机贝塞尔干扰曲线
    for _ in range(random.randint(4, 6)):
        x1 = random.randint(0, width // 3)
        y1 = random.randint(0, height)
        cx = random.randint(width // 4, width * 3 // 4)
        cy = random.randint(0, height)
        x2 = random.randint(width * 2 // 3, width)
        y2 = random.randint(0, height)
        stroke_color = f"rgba({random.randint(100, 180)}, {random.randint(100, 180)}, {random.randint(100, 180)}, {random.uniform(0.3, 0.6):.2f})"
        stroke_width = random.uniform(1.0, 2.0)
        svg_parts.append(
            f'<path d="M {x1} {y1} Q {cx} {cy} {x2} {y2}" fill="none" stroke="{stroke_color}" stroke-width="{stroke_width:.1f}" />'
        )

    # 绘制 25~35 个微小噪点
    for _ in range(random.randint(25, 35)):
        nx = random.randint(2, width - 2)
        ny = random.randint(2, height - 2)
        r = random.uniform(0.8, 1.8)
        color = f"rgba({random.randint(80, 160)}, {random.randint(80, 160)}, {random.randint(80, 160)}, {random.uniform(0.3, 0.7):.2f})"
        svg_parts.append(f'<circle cx="{nx}" cy="{ny}" r="{r:.1f}" fill="{color}" />')

    # 计算字符均匀分布
    char_count = len(text)
    col_width = (width - 20) / char_count

    # 常用深色文本颜色池，保证高辨识度与对比度
    dark_colors = [
        "#1e293b", "#0f172a", "#1e3a8a", "#14532d",
        "#7c2d12", "#4c1d95", "#831843", "#164e63",
    ]

    for i, char in enumerate(text):
        x = int(14 + i * col_width + random.uniform(-2, 4))
        y = int(height * 0.72 + random.uniform(-3, 3))
        angle = random.randint(-22, 22)
        font_size = random.randint(22, 26)
        char_color = random.choice(dark_colors)
        font_weight = random.choice(["bold", "600", "700"])

        svg_parts.append(
            f'<text x="{x}" y="{y}" font-family="system-ui, -apple-system, sans-serif, monospace" font-size="{font_size}" font-weight="{font_weight}" fill="{char_color}" transform="rotate({angle}, {x}, {y})">{char}</text>'
        )

    svg_parts.append("</svg>")
    return "".join(svg_parts)


def store_captcha(request: Request, text: str) -> None:
    """将验证码保存在当前用户 Session 中。"""
    request.session["captcha_code"] = text.upper()
    request.session["captcha_time"] = int(time.time())


def verify_captcha(request: Request, user_input: str | None) -> bool:
    """验证用户提交的验证码（验证后即刻作废，防止重放攻击）。"""
    expected = request.session.pop("captcha_code", None)
    created_at = request.session.pop("captcha_time", None)

    if not expected or not user_input or not created_at:
        return False

    # 验证是否超时
    if time.time() - int(created_at) > CAPTCHA_EXPIRY_SECONDS:
        return False

    return user_input.strip().upper() == expected
