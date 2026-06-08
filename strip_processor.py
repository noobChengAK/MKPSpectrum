"""
条带切割处理器：将预览层图切割为打印头条带，跳过空白区域，
生成分段PNG、Klipper风格G-code和timing，打包为ZIP。
"""
import os
import json
import zipfile
import math
import glob
from PIL import Image
import numpy as np

# ============================================================
# 物理常量
# ============================================================
BED_SIZE_MM = 270.0
HEAD_WIDTH_MM = 14.29
TRANSITION_MM = 2.15
TRANSITION_STEP_MM = 0.5
EFFECTIVE_STEP_MM = HEAD_WIDTH_MM - 2 * TRANSITION_MM  # 9.99 mm

# ============================================================
# 运动参数（硬编码，后续可改为传参）
# ============================================================
TRAVEL_SPEED_MM_S = 150.0    # 空驶速度 mm/s
TRAVEL_ACCEL_MM_S2 = 500.0   # 空驶加速度 mm/s²
PRINT_SPEED_MM_S = 60.0      # 打印速度 mm/s
HEAD_INITIAL_X_MM = 0.0      # 打印头初始位置 X
HEAD_INITIAL_Y_MM = 0.0      # 打印头初始位置 Y（左下角原点）

# ============================================================
# 工具函数
# ============================================================

def _mm_to_px(mm_val, scale):
    return mm_val * scale


def _px_to_mm(px_val, scale):
    return px_val / scale


def _calculate_travel_time(dist_mm):
    """计算空驶移动时间（梯形/三角形速度曲线）"""
    if dist_mm <= 0.001:
        return 0.0
    d_accel = (TRAVEL_SPEED_MM_S ** 2) / (2 * TRAVEL_ACCEL_MM_S2)
    if dist_mm <= 2 * d_accel:
        # 三角形：加速到一半后减速
        return 2.0 * math.sqrt(dist_mm / TRAVEL_ACCEL_MM_S2)
    else:
        # 梯形：加速 + 匀速 + 减速
        return (2.0 * TRAVEL_SPEED_MM_S / TRAVEL_ACCEL_MM_S2 +
                (dist_mm - 2 * d_accel) / TRAVEL_SPEED_MM_S)


# ============================================================
# 核心处理
# ============================================================

def _build_alpha_gradient(h, transition_px, step_px):
    """预计算 alpha 渐变乘数数组 (h,) —— 只算一次，后续直接乘。"""
    num_steps = max(1, int(math.ceil(TRANSITION_MM / TRANSITION_STEP_MM)))
    alpha = np.ones(h, dtype=np.float32)

    top_end = min(int(math.ceil(transition_px)), h)
    if top_end > 0 and step_px > 0:
        row_indices = np.arange(top_end)
        step_indices = np.clip((row_indices / step_px).astype(np.int32), 0, num_steps - 1)
        alpha[:top_end] = (step_indices + 1).astype(np.float32) / num_steps

    bottom_start = max(h - int(math.ceil(transition_px)), 0)
    if bottom_start < h and step_px > 0:
        row_indices = np.arange(bottom_start, h)
        dist_from_bottom = (h - 1 - row_indices)
        step_indices = np.clip((dist_from_bottom / step_px).astype(np.int32), 0, num_steps - 1)
        alpha[bottom_start:] = (step_indices + 1).astype(np.float32) / num_steps

    return alpha.reshape(-1, 1)  # (h, 1) 便于广播


def apply_alpha_gradient(strip_arr, alpha_gradient):
    """应用预计算的 alpha 渐变乘数（原地修改 float32 数组）。"""
    strip_arr[:, :, 3] = strip_arr[:, :, 3] * alpha_gradient
    return strip_arr


def find_non_white_blocks(strip_arr):
    """
    向量化检测：在条带图像中找出所有包含非白像素的列，合并为连续块。
    返回 [(x_start_px, x_end_px), ...] 列表。
    """
    if strip_arr.size == 0:
        return []

    # 非透明: alpha > 10
    opaque = strip_arr[:, :, 3] > 10  # (h, w) bool

    # 非白: R/G/B 不同时为 245+
    not_white = ~(
        (strip_arr[:, :, 0] > 245) &
        (strip_arr[:, :, 1] > 245) &
        (strip_arr[:, :, 2] > 245)
    )

    has_content = opaque & not_white  # (h, w) bool

    # 每列是否有任何内容像素
    col_has = np.any(has_content, axis=0)  # (w,) bool

    if not np.any(col_has):
        return []

    # 用 diff 找连续 True 段的边界
    padded = np.pad(col_has, (1, 1), constant_values=False)
    edges = np.diff(padded.astype(np.int8))
    starts = np.where(edges == 1)[0]
    ends = np.where(edges == -1)[0]

    return list(zip(starts.tolist(), ends.tolist()))


def _strip_intersects_region(strip_y_top_mm, strip_y_bottom_mm, regions):
    """检查条带 Y 范围是否与任一模型区域相交。regions: [{x, y, w, h}, ...] mm，y 为左下角。"""
    if not regions:
        return True  # 未提供区域，默认全部处理
    for r in regions:
        ry_bottom = r['y']
        ry_top = r['y'] + r['h']
        # 条带 [strip_y_bottom, strip_y_top] 与区域 [ry_bottom, ry_top] 有交集
        if strip_y_top_mm > ry_bottom and strip_y_bottom_mm < ry_top:
            return True
    return False


def _process_layer_core(full_arr, img_w, img_h, layer_name, bed_size_mm,
                         output_zip_dir, gcode_dir,
                         motion_params=None, model_regions=None):
    """核心：对内存中的图层数组执行条带切割、渐变、找块、timing、G-code。

    full_arr:  (img_h, img_w, 4) float32 RGBA numpy 数组
    img_w, img_h: 图像宽高（像素），必须相等
    bed_size_mm: 打印床尺寸 mm，用于 px↔mm 换算
    其他参数同 process_layer。
    """
    # 解析运动参数
    travel_speed = TRAVEL_SPEED_MM_S
    travel_accel = TRAVEL_ACCEL_MM_S2
    print_speed = PRINT_SPEED_MM_S
    head_init_x = HEAD_INITIAL_X_MM
    head_init_y = HEAD_INITIAL_Y_MM
    if motion_params:
        travel_speed = motion_params.get('travel_speed', travel_speed)
        travel_accel = motion_params.get('travel_accel', travel_accel)
        print_speed = motion_params.get('print_speed', print_speed)
        head_init_x = motion_params.get('head_initial_x', head_init_x)
        head_init_y = motion_params.get('head_initial_y', head_init_y)

    # 计算缩放比例
    scale = img_w / bed_size_mm  # px/mm
    head_width_px = _mm_to_px(HEAD_WIDTH_MM, scale)
    transition_px = _mm_to_px(TRANSITION_MM, scale)
    step_px = _mm_to_px(TRANSITION_STEP_MM, scale)
    effective_step_px = _mm_to_px(EFFECTIVE_STEP_MM, scale)

    # 计算条带数量
    remaining_mm = bed_size_mm
    strip_count = 0
    while remaining_mm > 0.01:
        remaining_mm -= EFFECTIVE_STEP_MM
        strip_count += 1

    # 准备输出
    layer_zip_dir = os.path.join(output_zip_dir, layer_name)
    os.makedirs(layer_zip_dir, exist_ok=True)

    all_blocks = []  # [{x_mm, y_mm, w_mm, h_mm, file, idx}]

    # 预计算标准条带高度的 alpha 渐变（所有条带高度相同）
    std_strip_h = min(int(round(head_width_px)), img_h)
    alpha_grad = _build_alpha_gradient(std_strip_h, transition_px, step_px)

    # ---- 按条带处理 ----
    for s in range(strip_count):
        # 条带在图像中的 y 范围（图像坐标：Y轴向下，顶部=0）
        # 条带 s 的顶部在 mm 坐标 = bed_size_mm - s * EFFECTIVE_STEP_MM
        # 转换成图像坐标：img_top_y_px = (bed_size_mm - strip_top_mm) * scale
        strip_top_mm = bed_size_mm - s * EFFECTIVE_STEP_MM  # 条带顶部在热床上的 mm 位置
        strip_bottom_mm = strip_top_mm - HEAD_WIDTH_MM        # 条带底部

        # 快速跳过：条带不与任何模型区域相交
        if model_regions and not _strip_intersects_region(strip_top_mm, strip_bottom_mm, model_regions):
            continue

        # 图像中条带顶部对应的像素行
        strip_img_top_px = int(round((bed_size_mm - strip_top_mm) * scale))
        # 图像中条带底部对应的像素行
        strip_img_bottom_px = int(round(strip_img_top_px + head_width_px))

        # 边界裁剪
        strip_img_top_px = max(0, strip_img_top_px)
        strip_img_bottom_px = min(img_h, strip_img_bottom_px)

        if strip_img_bottom_px <= strip_img_top_px:
            continue

        # 提取条带并应用 alpha 渐变
        strip_arr = full_arr[strip_img_top_px:strip_img_bottom_px, :, :].copy()
        strip_h = strip_arr.shape[0]
        # 边界条带可能高度不同，按需重算 alpha 渐变
        grad = alpha_grad if strip_h == std_strip_h else _build_alpha_gradient(strip_h, transition_px, step_px)
        strip_arr = apply_alpha_gradient(strip_arr, grad)

        # 转为 uint8 用于找块和保存
        strip_arr_uint8 = strip_arr.clip(0, 255).astype(np.uint8)

        # 找非白块
        blocks = find_non_white_blocks(strip_arr_uint8)
        if not blocks:
            continue

        # 条带中心在热床上的 Y 坐标
        strip_center_y_mm = strip_top_mm - HEAD_WIDTH_MM / 2.0

        for bi, (x_start_px, x_end_px) in enumerate(blocks):
            # 裁剪块图像
            x_start_px = max(0, int(x_start_px))
            x_end_px = min(strip_arr_uint8.shape[1], int(x_end_px))
            block_img_arr = strip_arr_uint8[:, x_start_px:x_end_px, :]
            block_img = Image.fromarray(block_img_arr, 'RGBA')

            # 块的文件名
            block_file = f"s{s:02d}_b{bi:03d}.png"
            block_path = os.path.join(layer_zip_dir, block_file)
            block_img.save(block_path, 'PNG')

            # 块在热床上的位置（mm）
            x_mm = _px_to_mm(x_start_px, scale)
            w_mm = _px_to_mm(x_end_px - x_start_px, scale)
            y_mm = strip_center_y_mm
            h_mm = HEAD_WIDTH_MM

            all_blocks.append({
                'x_mm': round(x_mm, 3),
                'y_mm': round(y_mm, 3),
                'w_mm': round(w_mm, 3),
                'h_mm': round(h_mm, 3),
                'file': block_file,
                'strip': s,
            })

    # ---- 计算 timing ----
    prev_end_x = head_init_x
    prev_end_y = head_init_y
    block_timings = []

    for i, blk in enumerate(all_blocks):
        dx = blk['x_mm'] - prev_end_x
        dy = blk['y_mm'] - prev_end_y
        dist = math.sqrt(dx * dx + dy * dy)
        travel_time = _calculate_travel_time(dist)

        block_timings.append({
            'file': blk['file'],
            'x_mm': blk['x_mm'],
            'y_mm': blk['y_mm'],
            'w_mm': blk['w_mm'],
            'travel_s': round(travel_time, 4),
        })

        prev_end_x = blk['x_mm'] + blk['w_mm']
        prev_end_y = blk['y_mm']

    # 写入 timing.json
    timing_data = {
        'layer_name': layer_name,
        'total_blocks': len(all_blocks),
        'initial_wait_s': round(block_timings[0]['travel_s'] if block_timings else 0, 4),
        'blocks': block_timings,
    }
    timing_path = os.path.join(layer_zip_dir, 'timing.json')
    with open(timing_path, 'w', encoding='utf-8') as f:
        json.dump(timing_data, f, indent=2, ensure_ascii=False)

    # ---- 生成 Klipper 风格 G-code ----
    os.makedirs(gcode_dir, exist_ok=True)
    gcode_path = os.path.join(gcode_dir, f"{layer_name}.gcode")
    travel_feedrate = int(travel_speed * 60)   # mm/s → mm/min
    print_feedrate = int(print_speed * 60)

    gcode_lines = [
        f"; Layer: {layer_name}",
        f"; Strips: {strip_count}, Blocks: {len(all_blocks)}",
        f"; Head: {HEAD_WIDTH_MM}mm, Transition: {TRANSITION_MM}mm",
        f"; Travel speed: {travel_speed}mm/s, Print speed: {print_speed}mm/s",
        "; ---",
    ]

    for blk, tmg in zip(all_blocks, block_timings):
        x_start = blk['x_mm']
        x_end = blk['x_mm'] + blk['w_mm']
        y_pos = blk['y_mm']

        gcode_lines.append(
            f"G0 X{x_start:.3f} Y{y_pos:.3f} F{travel_feedrate}"
            f"  ; travel {tmg['travel_s']:.4f}s (block: {blk['file']})"
        )
        gcode_lines.append(
            f"G1 X{x_end:.3f} Y{y_pos:.3f} F{print_feedrate}"
            f"  ; print {blk['w_mm']:.3f}mm"
        )

    with open(gcode_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(gcode_lines) + '\n')

    return {
        'layer_name': layer_name,
        'strip_count': strip_count,
        'total_blocks': len(all_blocks),
        'gcode_path': gcode_path,
        'zip_subdir': layer_name,
    }


def process_layer(image_path, output_zip_dir, gcode_dir,
                  motion_params=None, model_regions=None):
    """
    处理单张图层图片（从文件读取）。

    参数：
        image_path:    图层 PNG 路径
        output_zip_dir: ZIP 解压后的根目录
        gcode_dir:     G-code 输出目录
        motion_params: 可选 dict，覆盖默认运动参数
        model_regions: 可选 [{x, y, w, h}, ...]，mm 坐标（左下角原点）
    返回：
        dict: {layer_name, strip_count, total_blocks, gcode_path, zip_subdir}
    """
    img = Image.open(image_path).convert('RGBA')
    img_w, img_h = img.size
    if img_w != img_h:
        raise ValueError(f"图像必须为正方形，当前尺寸: {img_w}x{img_h}")

    layer_name = os.path.splitext(os.path.basename(image_path))[0]
    full_arr = np.array(img).astype(np.float32)

    return _process_layer_core(
        full_arr, img_w, img_h, layer_name, BED_SIZE_MM,
        output_zip_dir, gcode_dir,
        motion_params=motion_params, model_regions=model_regions,
    )


def process_layer_from_array(arr, layer_name, img_size, bed_size_mm,
                              output_zip_dir, gcode_dir,
                              motion_params=None, model_regions=None):
    """处理内存中的图层数组（跳过 PNG 文件读写）。

    arr:          (img_size, img_size, 4) uint8 或 float32 RGBA numpy 数组
    layer_name:   图层名称（如 "layer_0000"）
    img_size:     图像边长（像素，必须与 arr.shape[:2] 一致）
    bed_size_mm:  打印床尺寸 mm
    其他参数同 process_layer。
    """
    h, w = arr.shape[:2]
    if w != h or w != img_size:
        raise ValueError(
            f"数组尺寸 ({w}x{h}) 与 img_size ({img_size}) 不匹配"
        )
    full_arr = arr.astype(np.float32, copy=False)
    return _process_layer_core(
        full_arr, img_size, img_size, layer_name, bed_size_mm,
        output_zip_dir, gcode_dir,
        motion_params=motion_params, model_regions=model_regions,
    )


def process_all_layers(preview_dir, output_base_dir, motion_params=None,
                       model_regions=None):
    """
    处理 Preview 目录下所有图层 PNG。

    输出结构：
        output_base_dir/
          ├── Segment/
          │   ├── gcode/
          │   │   ├── layer_0000.gcode
          │   │   └── ...
          │   └── zip/
          │       └── segments.zip
          │           ├── layer_0000/
          │           │   ├── timing.json
          │           │   ├── s00_b000.png
          │           │   └── ...
          │           └── layer_0001/
          │               └── ...

    参数：
        preview_dir:    预览图目录
        output_base_dir: 输出根目录（其下创建 Segment/...）
        motion_params:   运动参数字典
        model_regions:   可选 [{x, y, w, h}, ...]，mm 坐标，只处理相交条带
    返回：
        dict: {success, layers, total_blocks, zip_path}
    """
    segment_dir = os.path.join(output_base_dir, 'Segment')
    gcode_dir = os.path.join(segment_dir, 'gcode')
    zip_dir = os.path.join(segment_dir, 'zip')
    temp_dir = os.path.join(segment_dir, '_temp')  # 临时打包目录

    # 清理旧输出
    import shutil
    for d in [gcode_dir, zip_dir, temp_dir]:
        if os.path.exists(d):
            shutil.rmtree(d, ignore_errors=True)
    os.makedirs(gcode_dir, exist_ok=True)
    os.makedirs(zip_dir, exist_ok=True)
    os.makedirs(temp_dir, exist_ok=True)

    # 找到所有 PNG 并排序
    png_files = sorted(
        glob.glob(os.path.join(preview_dir, '*.png'))
    )
    if not png_files:
        return {'success': False, 'error': 'Preview 目录中没有 PNG 文件'}

    results = []
    total_blocks = 0

    for png_path in png_files:
        r = process_layer(
            png_path,
            output_zip_dir=temp_dir,
            gcode_dir=gcode_dir,
            motion_params=motion_params,
            model_regions=model_regions,
        )
        results.append(r)
        total_blocks += r['total_blocks']
        print(f"  [{r['layer_name']}] strips={r['strip_count']}, "
              f"blocks={r['total_blocks']}")

    # ---- 打包为单个大 ZIP ----
    zip_path = os.path.join(zip_dir, 'segments.zip')
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(temp_dir):
            for fname in files:
                full_path = os.path.join(root, fname)
                arcname = os.path.relpath(full_path, temp_dir)
                zf.write(full_path, arcname)

    # 清理临时目录
    shutil.rmtree(temp_dir, ignore_errors=True)

    return {
        'success': True,
        'layers': [r['layer_name'] for r in results],
        'total_layers': len(results),
        'total_blocks': total_blocks,
        'zip_path': zip_path,
        'gcode_dir': gcode_dir,
    }


if __name__ == '__main__':
    # 测试运行
    import sys
    preview_dir = os.path.expanduser(
        '~/Documents/MKPSpectrum/Temp/Texture/Preview'
    )
    output_dir = os.path.expanduser(
        '~/Documents/MKPSpectrum/Temp/Texture'
    )
    result = process_all_layers(preview_dir, output_dir)
    print(json.dumps(result, indent=2, ensure_ascii=False))
