# /// script
# requires-python = ">=3.10"
# dependencies = ["pillow>=11,<13"]
# ///
"""Review evidence for anime-pv: filmstrips, same-time comparisons, cut lists and
engineering checks. Outputs support review; they never replace continuous playback
review or user acceptance."""
import argparse
import bisect
import io
import json
import math
import re
import shutil
import statistics
import subprocess
from fractions import Fraction
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageOps

SHOWINFO = re.compile(r'\bn:\s*\d+\s+pts:\s*-?\d+\s+pts_time:\s*(-?[\d.]+)')
BLACK = re.compile(r'black_start:\s*(-?[\d.]+)\s+black_end:\s*(-?[\d.]+)\s+black_duration:\s*([\d.]+)')
FREEZE = re.compile(r'lavfi\.freezedetect\.freeze_(start|end):\s*(-?[\d.]+)')
EVIDENCE = 'Static frames only; not a substitute for continuous playback review.'
TIMING_TOLERANCE = 0.0015


class MediaError(Exception):
    """Safe user-facing media error."""


def executable(name):
    path = shutil.which(name)
    if not path:
        raise MediaError(f'{name} missing; run project.py preflight for installation guidance.')
    return path


def run(command, binary=False, timeout=1800):
    try:
        return subprocess.run(command, capture_output=True, text=not binary,
                              errors=None if binary else 'replace', timeout=timeout)
    except subprocess.TimeoutExpired:
        raise MediaError('Media command timed out; narrow the time range.') from None


def probe(path):
    result = run([executable('ffprobe'), '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(path)])
    if result.returncode:
        raise MediaError(f'ffprobe failed for {Path(path).name}; check that it exists and decodes.')
    return json.loads(result.stdout)


def video_stream(info):
    for stream in info.get('streams', []):
        if stream.get('codec_type') == 'video' and not stream.get('disposition', {}).get('attached_pic'):
            return stream
    raise MediaError('No video stream found.')


def audio_streams(info):
    return [stream for stream in info.get('streams', []) if stream.get('codec_type') == 'audio']


def ratio(text):
    try:
        value = Fraction(str(text).replace(':', '/'))
    except (ValueError, ZeroDivisionError):
        return None
    return float(value) if value > 0 else None


def frame_rate(stream):
    return ratio(stream.get('avg_frame_rate')) or ratio(stream.get('r_frame_rate'))


def start_time(info):
    try:
        return float(info.get('format', {}).get('start_time') or 0)
    except ValueError:
        return 0.0


def frame_times(path, info):
    """Presentation times of displayed video frames, in seconds from the file start."""
    stream, offset = video_stream(info), start_time(info)
    base = [executable('ffprobe'), '-v', 'error', '-select_streams', str(stream['index']), '-of', 'json']
    packets = json.loads(run(base + ['-show_entries', 'packet=pts_time,flags', str(path)]).stdout or '{}')
    values = [p.get('pts_time') for p in packets.get('packets', []) if 'D' not in p.get('flags', '')]
    if not values or any(value in (None, 'N/A') for value in values):
        frames = json.loads(run(base + ['-show_entries', 'frame=best_effort_timestamp_time', str(path)]).stdout or '{}')
        values = [f.get('best_effort_timestamp_time') for f in frames.get('frames', [])]
    try:
        times = sorted({round(float(value) - offset, 6) for value in values if value not in (None, 'N/A')})
    except ValueError:
        times = []
    if not times:
        raise MediaError(f'Cannot read video frame timestamps from {Path(path).name}.')
    return times


def video_end(stream, times):
    return times[-1] + 1 / (frame_rate(stream) or 24)


def displayed_index(times, moment):
    """Frame shown at `moment`: the last frame starting at or before it (safe for VFR)."""
    return max(bisect.bisect_right(times, moment + 1e-6) - 1, 0)


def seek_point(times, index):
    # Seeking to the midpoint lands exactly on frame `index` regardless of float rounding.
    return None if index <= 0 else (times[index - 1] + times[index]) / 2


def scale_filter(stream, width):
    sar = ratio(stream.get('sample_aspect_ratio'))
    square = '' if sar in (None, 1.0) else f'scale=iw*{sar:.6f}:ih,setsar=1,'
    return square + f'scale={width}:-2:flags=lanczos'


def frame_image(path, stream, times, index, width):
    command = [executable('ffmpeg'), '-hide_banner', '-nostdin', '-v', 'error']
    seek = seek_point(times, index)
    if seek is not None:
        command += ['-ss', f'{seek:.6f}']
    command += ['-i', str(path), '-map', f'0:{stream["index"]}', '-frames:v', '1',
                '-vf', scale_filter(stream, width), '-f', 'image2pipe', '-c:v', 'png', '-']
    result = run(command, binary=True)
    if result.returncode or not result.stdout:
        raise MediaError(f'Cannot extract frame {index} from {Path(path).name}.')
    with Image.open(io.BytesIO(result.stdout)) as image:
        return image.convert('RGB')


def parse_time(text):
    total = 0.0
    for part in str(text).strip().split(':'):
        total = total * 60 + float(part)
    return total


def sample_times(start, end, fps=None, count=None):
    if end <= start:
        raise MediaError('End time must be after start time.')
    if fps:
        steps = max(int(math.floor((end - start) * fps + 1e-9)), 1)
        return [start + index / fps for index in range(steps)]
    return [start + (index + 0.5) * (end - start) / count for index in range(count)]


def pad(image, size):
    if image.size == size:
        return image
    canvas = Image.new('RGB', size, 'black')
    canvas.paste(image, ((size[0] - image.width) // 2, (size[1] - image.height) // 2))
    return canvas


def heatmap(reference, candidate):
    difference = ImageChops.difference(reference, candidate).convert('L').point(lambda value: min(255, value * 4))
    return ImageOps.colorize(difference, black='#000000', mid='#d32f2f', white='#ffeb3b')


def labeled_grid(tiles, columns, title):
    """Lay out (image, label) tiles with a label strip under each and a title line on top."""
    cell_w = max(image.width for image, _ in tiles)
    cell_h = max(image.height for image, _ in tiles)
    font = ImageFont.load_default(size=max(12, cell_w // 26))
    label_h = ImageDraw.Draw(Image.new('RGB', (1, 1))).textbbox((0, 0), 'Ag0', font=font)[3] + 8
    gap, rows = 4, math.ceil(len(tiles) / columns)
    sheet = Image.new('RGB', (gap + columns * (cell_w + gap), label_h + gap + rows * (cell_h + label_h + gap)), '#1e1e1e')
    draw = ImageDraw.Draw(sheet)
    draw.text((gap, 3), title, fill='#f0f0f0', font=font)
    for number, (image, label) in enumerate(tiles):
        x = gap + (number % columns) * (cell_w + gap)
        y = label_h + gap + (number // columns) * (cell_h + label_h + gap)
        sheet.paste(image, (x + (cell_w - image.width) // 2, y + (cell_h - image.height) // 2))
        draw.text((x + 2, y + cell_h + 3), label, fill='#f0f0f0', font=font)
    return sheet


def save(image, output):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output, 'PNG')
    return str(output.resolve())


def check_output(output, overwrite, width, columns=1):
    if Path(output).exists() and not overwrite:
        raise MediaError('Output exists; use a new version name or pass --overwrite.')
    if width < 16 or columns < 1:
        raise MediaError('Width must be at least 16 pixels and columns at least 1.')


def sheet(video, output, start=0.0, end=None, fps=None, count=None, every_frame=False,
          width=480, columns=6, max_frames=120, overwrite=False):
    check_output(output, overwrite, width, columns)
    info = probe(video)
    stream, times = video_stream(info), frame_times(video, info)
    end = video_end(stream, times) if end is None else min(end, video_end(stream, times))
    if every_frame:
        indices = [index for index, moment in enumerate(times) if start - 1e-6 <= moment < end - 1e-6]
        mode = 'every frame'
    else:
        if (fps is not None and fps <= 0) or (count is not None and count <= 0):
            raise MediaError('--fps and --count must be positive.')
        indices = [displayed_index(times, moment) for moment in sample_times(start, end, fps, count or 24)]
        mode = f'{fps:g} fps' if fps else f'{len(indices)} samples'
    if not indices:
        raise MediaError('No frames in the requested range.')
    if len(indices) > max_frames:
        raise MediaError(f'{len(indices)} frames exceed --max-frames {max_frames}; narrow the range or sample less.')
    cache = {}
    for index in indices:
        if index not in cache:
            cache[index] = frame_image(video, stream, times, index, width)
    tiles = [(cache[index], f'{times[index]:.3f}s #{index}') for index in indices]
    title = f'{Path(video).name}  {start:.3f}-{end:.3f}s  {mode}'
    return {'output': save(labeled_grid(tiles, columns, title), output),
            'video': str(Path(video).resolve()), 'columns': columns,
            'frames': [{'index': index, 'time': times[index]} for index in indices], 'evidence': EVIDENCE}


def compare(reference, candidate, output, times=None, start=0.0, end=None, fps=None, count=None,
            width=480, diff=False, max_frames=60, overwrite=False):
    check_output(output, overwrite, width)
    ref_info, out_info = probe(reference), probe(candidate)
    ref_stream, out_stream = video_stream(ref_info), video_stream(out_info)
    ref_times, out_times = frame_times(reference, ref_info), frame_times(candidate, out_info)
    if times is None:
        if (fps is not None and fps <= 0) or (count is not None and count <= 0):
            raise MediaError('--fps and --count must be positive.')
        times = sample_times(start, video_end(ref_stream, ref_times) if end is None else end, fps, count or 12)
    if not times or len(times) > max_frames:
        raise MediaError(f'Need 1..{max_frames} comparison times; narrow the range or sample less.')
    tiles, rows, warnings = [], [], set()
    for moment in times:
        if moment >= video_end(out_stream, out_times) or moment >= video_end(ref_stream, ref_times):
            warnings.add('time_beyond_video_end')
        i, j = displayed_index(ref_times, moment), displayed_index(out_times, moment)
        a = frame_image(reference, ref_stream, ref_times, i, width)
        b = frame_image(candidate, out_stream, out_times, j, width)
        if a.size != b.size:
            warnings.add('frame_size_or_aspect_mismatch')
            size = (max(a.width, b.width), max(a.height, b.height))
            a, b = pad(a, size), pad(b, size)
        tiles += [(a, f'REF {moment:.3f}s #{i}'), (b, f'OUT {moment:.3f}s #{j}')]
        if diff:
            tiles.append((heatmap(a, b), 'DIFF locator'))
        rows.append({'time': moment, 'reference_frame': i, 'candidate_frame': j})
    title = f'{Path(reference).name} vs {Path(candidate).name}'
    return {'output': save(labeled_grid(tiles, 3 if diff else 2, title), output),
            'reference': str(Path(reference).resolve()), 'candidate': str(Path(candidate).resolve()),
            'rows': rows, 'diff': diff, 'warnings': sorted(warnings),
            'evidence': EVIDENCE + ' The diff only locates differences; it is not a similarity score.'}


def parse_showinfo(stderr):
    return [float(value) for value in SHOWINFO.findall(stderr)]


def detect_cuts(video, threshold):
    if not 0 < threshold < 1:
        raise MediaError('--threshold must be between 0 and 1.')
    info = probe(video)
    stream = video_stream(info)
    result = run([executable('ffmpeg'), '-hide_banner', '-nostdin', '-nostats', '-v', 'info', '-i', str(video),
                  '-map', f'0:{stream["index"]}', '-vf', f"select='gt(scene,{threshold})',showinfo", '-f', 'null', '-'])
    if result.returncode:
        raise MediaError(f'Scene detection failed for {Path(video).name}.')
    return info, stream, sorted(parse_showinfo(result.stderr))


def shot_stats(found, end):
    bounds = [0.0] + [moment for moment in found if 0 < moment < end] + [end]
    lengths = [round(b - a, 6) for a, b in zip(bounds, bounds[1:]) if b > a]
    return {'count': len(lengths), 'lengths': lengths, 'median': statistics.median(lengths) if lengths else None,
            'shortest': min(lengths, default=None), 'longest': max(lengths, default=None)}


def match_cuts(reference, candidate, tolerance):
    matched, missing, used = [], [], set()
    for ref in reference:
        best = None
        for index, moment in enumerate(candidate):
            delta = moment - ref
            if index not in used and abs(delta) <= tolerance + 1e-6 and (best is None or abs(delta) < abs(best[1])):
                best = (index, delta)
        if best is None:
            missing.append(ref)
        else:
            used.add(best[0])
            matched.append({'reference': ref, 'candidate': candidate[best[0]], 'offset_seconds': round(best[1], 6)})
    return matched, missing, [moment for index, moment in enumerate(candidate) if index not in used]


def cuts(video, threshold=0.3, reference=None, tolerance_frames=1):
    info, stream, found = detect_cuts(video, threshold)
    times = frame_times(video, info)
    result = {'video': str(Path(video).resolve()), 'threshold': threshold, 'fps': frame_rate(stream),
              'cuts': [{'time': moment, 'frame': displayed_index(times, moment)} for moment in found],
              'shots': shot_stats(found, video_end(stream, times)),
              'note': 'Scene-score cuts are locators: flashes can add cuts and dissolves can be missed.'}
    if reference:
        _, ref_stream, ref_found = detect_cuts(reference, threshold)
        fps = frame_rate(ref_stream) or 24
        matched, missing, extra = match_cuts(ref_found, found, tolerance_frames / fps)
        for item in matched:
            item['offset_frames'] = round(item['offset_seconds'] * fps, 2)
        result['reference'] = {'video': str(Path(reference).resolve()), 'cuts': len(ref_found),
                               'tolerance_frames': tolerance_frames, 'matched': matched, 'missing': missing,
                               'extra': extra, 'status': 'review' if missing or extra else 'aligned'}
    return result


def check(name, status, detail):
    return {'name': name, 'status': status, 'detail': detail}


def decode_check(video):
    result = run([executable('ffmpeg'), '-hide_banner', '-nostdin', '-v', 'error', '-i', str(video),
                  '-map', '0:v?', '-map', '0:a?', '-f', 'null', '-'])
    errors = [line.strip() for line in result.stderr.splitlines() if line.strip()]
    status = 'pass' if result.returncode == 0 and not errors else 'fail'
    return check('decode', status, {'error_lines': len(errors), 'first_errors': errors[:5]})


def stream_check(stream, info):
    keys = ('codec_name', 'width', 'height', 'pix_fmt', 'sample_aspect_ratio', 'avg_frame_rate', 'r_frame_rate',
            'color_space', 'color_primaries', 'color_transfer', 'color_range', 'field_order')
    detail = {key: stream.get(key) for key in keys}
    detail['container'] = info.get('format', {}).get('format_name')
    detail['untagged_color'] = [key for key in ('color_space', 'color_primaries', 'color_transfer')
                                if stream.get(key) in (None, 'unknown')]
    interlaced = stream.get('field_order') not in (None, 'progressive', 'unknown')
    return check('video-stream', 'review' if detail['untagged_color'] or interlaced else 'pass', detail)


def timing_check(stream, times):
    fps = frame_rate(stream)
    step = 1 / fps if fps else None
    deltas = [b - a for a, b in zip(times, times[1:])]
    gaps = [{'after': a, 'seconds': round(d, 6)} for a, d in zip(times, deltas) if step and d > step * 1.5]
    variable = bool(deltas) and step is not None and max(deltas) - min(deltas) > step * 0.5
    detail = {'frames': len(times), 'nominal_fps': fps, 'first_frame': times[0], 'gaps': len(gaps),
              'first_gaps': gaps[:5], 'variable_timing': variable}
    return check('frame-timing', 'review' if gaps or variable else 'pass', detail)


def parse_black_freeze(stderr):
    black = [{'start': float(a), 'end': float(b), 'duration': float(c)} for a, b, c in BLACK.findall(stderr)]
    freeze, current = [], None
    for kind, value in FREEZE.findall(stderr):
        if kind == 'start':
            current = float(value)
        elif current is not None:
            freeze.append({'start': current, 'end': float(value)})
            current = None
    if current is not None:
        freeze.append({'start': current, 'end': None})
    return black, freeze


def black_freeze_check(video, stream):
    result = run([executable('ffmpeg'), '-hide_banner', '-nostdin', '-nostats', '-v', 'info', '-i', str(video),
                  '-map', f'0:{stream["index"]}', '-vf', 'blackdetect=d=0.04:pix_th=0.10,freezedetect=n=0.001:d=1',
                  '-f', 'null', '-'])
    if result.returncode:
        return check('black-and-freeze', 'review', {'error': 'Detection did not run; review playback manually.'})
    black, freeze = parse_black_freeze(result.stderr)
    return check('black-and-freeze', 'review' if black or freeze else 'pass',
                 {'black': black[:10], 'freeze': freeze[:10], 'black_events': len(black), 'freeze_events': len(freeze),
                  'meaning': 'Events can be intentional (black cuts, held cards); confirm them in playback.'})


def timeline_check(stream, times, ref_stream, ref_times):
    fps, ref_fps = frame_rate(stream), frame_rate(ref_stream)
    step = 1 / (ref_fps or 24)
    delta = video_end(stream, times) - video_end(ref_stream, ref_times)
    detail = {'size': [stream.get('width'), stream.get('height')],
              'reference_size': [ref_stream.get('width'), ref_stream.get('height')],
              'fps': fps, 'reference_fps': ref_fps, 'frames': len(times), 'reference_frames': len(ref_times),
              'duration_delta_frames': round(delta / step, 2),
              'meaning': 'Differences may be approved output specs; judge against the recorded decision.'}
    same = (detail['size'] == detail['reference_size'] and fps and ref_fps and abs(fps - ref_fps) < 1e-3
            and abs(delta) <= step + 1e-6)
    return check('reference-timeline', 'pass' if same else 'review', detail)


def parse_framemd5(text):
    timebase, packets = None, []
    for line in text.splitlines():
        if line.startswith('#tb'):
            timebase = Fraction(line.split(':', 1)[1].strip())
        elif line and not line.startswith('#'):
            fields = [field.strip() for field in line.split(',')]
            if len(fields) >= 6:
                # stream, dts, pts, duration, size, hash[, side data...]
                packets.append((int(fields[2]), int(fields[3]), int(fields[4]), fields[5]))
    if timebase is None:
        raise MediaError('Unexpected framemd5 output.')
    return timebase, packets


def packet_list(path, position, codec):
    command = [executable('ffmpeg'), '-hide_banner', '-nostdin', '-v', 'error', '-i', str(path),
               '-map', f'0:a:{position}', '-c', 'copy']
    if codec == 'aac':
        # ADTS headers are container framing; hash the raw AAC payload on both sides.
        command += ['-bsf:a', 'aac_adtstoasc']
    result = run(command + ['-f', 'framemd5', '-'])
    if result.returncode:
        raise MediaError(f'Cannot read audio packets from {Path(path).name}.')
    return parse_framemd5(result.stdout)


def compare_packets(ref, out, ref_video_start, video_start, frame_step):
    """Compare two (timebase, packets) lists: payload identity, sync offset and packet timing."""
    (ref_tb, ref_packets), (tb, packets) = ref, out
    first = next((i for i, (a, b) in enumerate(zip(ref_packets, packets)) if a[2:] != b[2:]), None)
    if first is None and len(ref_packets) != len(packets):
        first = min(len(ref_packets), len(packets))
    identical = first is None and bool(packets)
    detail = {'packets': len(packets), 'reference_packets': len(ref_packets),
              'identical_payload': identical, 'first_mismatch': first}
    if not (packets and ref_packets):
        return 'fail', detail
    start, ref_start = float(packets[0][0] * tb), float(ref_packets[0][0] * ref_tb)
    sync = (start - video_start) - (ref_start - ref_video_start)
    detail['sync_offset_delta_seconds'] = round(sync, 6)
    end = float((packets[-1][0] + packets[-1][1]) * tb) - start
    detail['duration_delta_seconds'] = round(end - (float((ref_packets[-1][0] + ref_packets[-1][1]) * ref_tb) - ref_start), 6)
    deviation = max(abs((float(p[0] * tb) - start) - (float(r[0] * ref_tb) - ref_start)) for p, r in zip(packets, ref_packets))
    detail['max_timing_deviation_seconds'] = round(deviation, 6)
    if not identical or deviation > TIMING_TOLERANCE or abs(sync) > frame_step:
        return 'fail', detail
    if abs(sync) > TIMING_TOLERANCE:
        detail['hint'] = ('Sub-frame sync shift, often encoder priming or a container start offset; '
                          'align the video timestamps if exact replica timing is required.')
        return 'review', detail
    return 'pass', detail


def audio_copy_check(video, info, times, reference, ref_info, ref_times, ref_stream):
    ours, theirs = audio_streams(info), audio_streams(ref_info)
    if len(ours) != len(theirs):
        return check('audio-copy', 'fail', {'audio_streams': len(ours), 'reference_audio_streams': len(theirs)})
    frame_step = 1 / (frame_rate(ref_stream) or 24)
    streams = []
    for position, (ref_audio, audio) in enumerate(zip(theirs, ours)):
        status, detail = compare_packets(packet_list(reference, position, ref_audio.get('codec_name')),
                                         packet_list(video, position, audio.get('codec_name')),
                                         ref_times[0], times[0], frame_step)
        differences = [key for key in ('codec_name', 'profile', 'sample_rate', 'channels', 'channel_layout')
                       if audio.get(key) != ref_audio.get(key)]
        if audio.get('tags', {}).get('language') != ref_audio.get('tags', {}).get('language'):
            differences.append('tag:language')
        differences += ['disposition:' + key for key in ('default', 'forced')
                        if audio.get('disposition', {}).get(key) != ref_audio.get('disposition', {}).get(key)]
        detail['metadata_differences'] = differences
        if status == 'pass' and differences:
            status = 'review'
        streams.append({'position': position, 'codec': audio.get('codec_name'), 'status': status, **detail})
    statuses = {item['status'] for item in streams}
    status = 'fail' if 'fail' in statuses else 'review' if 'review' in statuses else 'pass'
    return check('audio-copy', status, {'audio_streams': len(ours), 'streams': streams})


def verify(video, reference=None, expect_audio=None):
    if expect_audio == 'copy' and not reference:
        raise MediaError('--expect-audio copy needs --reference.')
    info = probe(video)
    stream, times = video_stream(info), frame_times(video, info)
    checks = [decode_check(video), stream_check(stream, info), timing_check(stream, times),
              black_freeze_check(video, stream)]
    if reference:
        ref_info = probe(reference)
        ref_stream, ref_times = video_stream(ref_info), frame_times(reference, ref_info)
        checks.append(timeline_check(stream, times, ref_stream, ref_times))
    if expect_audio == 'none':
        count = len(audio_streams(info))
        checks.append(check('audio-none', 'pass' if count == 0 else 'fail', {'audio_streams': count}))
    elif expect_audio == 'copy':
        checks.append(audio_copy_check(video, info, times, reference, ref_info, ref_times, ref_stream))
    statuses = {item['status'] for item in checks}
    return {'layer': 'engineering', 'video': str(Path(video).resolve()),
            'status': 'fail' if 'fail' in statuses else 'review' if 'review' in statuses else 'pass',
            'checks': checks,
            'note': 'Engineering layer only; visual/motion review and user acceptance are recorded separately.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    strip = commands.add_parser('sheet', help='Labeled filmstrip/contact sheet of one video')
    strip.add_argument('video', type=Path)
    strip.add_argument('--output', type=Path, required=True)
    strip.add_argument('--start', type=parse_time, default=0.0)
    strip.add_argument('--end', type=parse_time)
    mode = strip.add_mutually_exclusive_group()
    mode.add_argument('--fps', type=float)
    mode.add_argument('--count', type=int, help='Evenly spaced samples (default 24)')
    mode.add_argument('--every-frame', action='store_true')
    strip.add_argument('--width', type=int, default=480)
    strip.add_argument('--columns', type=int, default=6)
    strip.add_argument('--max-frames', type=int, default=120)
    strip.add_argument('--overwrite', action='store_true')
    pair = commands.add_parser('compare', help='Same display time side by side, optional diff locator')
    pair.add_argument('reference', type=Path)
    pair.add_argument('candidate', type=Path)
    pair.add_argument('--output', type=Path, required=True)
    moments = pair.add_mutually_exclusive_group()
    moments.add_argument('--times', help='Comma-separated times, seconds or m:ss.sss')
    moments.add_argument('--fps', type=float)
    moments.add_argument('--count', type=int, help='Evenly spaced samples (default 12)')
    pair.add_argument('--start', type=parse_time, default=0.0)
    pair.add_argument('--end', type=parse_time)
    pair.add_argument('--width', type=int, default=480)
    pair.add_argument('--diff', action='store_true')
    pair.add_argument('--max-frames', type=int, default=60)
    pair.add_argument('--overwrite', action='store_true')
    cut = commands.add_parser('cuts', help='Scene-cut locator, shot lengths and optional reference alignment')
    cut.add_argument('video', type=Path)
    cut.add_argument('--threshold', type=float, default=0.3)
    cut.add_argument('--reference', type=Path)
    cut.add_argument('--tolerance-frames', type=float, default=1)
    check_parser = commands.add_parser('verify', help='Engineering checks, optional reference timeline and audio copy')
    check_parser.add_argument('video', type=Path)
    check_parser.add_argument('--reference', type=Path)
    check_parser.add_argument('--expect-audio', choices=['none', 'copy'])
    args = parser.parse_args()
    try:
        if args.command == 'sheet':
            result = sheet(args.video, args.output, args.start, args.end, args.fps, args.count, args.every_frame,
                           args.width, args.columns, args.max_frames, args.overwrite)
        elif args.command == 'compare':
            times = [parse_time(value) for value in args.times.split(',') if value.strip()] if args.times else None
            result = compare(args.reference, args.candidate, args.output, times, args.start, args.end, args.fps,
                             args.count, args.width, args.diff, args.max_frames, args.overwrite)
        elif args.command == 'cuts':
            result = cuts(args.video, args.threshold, args.reference, args.tolerance_frames)
        else:
            result = verify(args.video, args.reference, args.expect_audio)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (MediaError, OSError, ValueError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
