import io
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageChops
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import media as m

HAS_FFMPEG = bool(shutil.which('ffmpeg') and shutil.which('ffprobe'))


class MediaLogicTest(unittest.TestCase):
    def test_display_time_selection_survives_rounding_and_vfr(self):
        times = [round(index / 24, 6) for index in range(48)]
        self.assertEqual(m.displayed_index(times, 1 / 24), 1)
        self.assertEqual(m.displayed_index(times, 0.06), 1)
        self.assertEqual(m.displayed_index(times, -1), 0)
        self.assertEqual(m.displayed_index([0.0, 0.5, 3.0], 2.9), 1)
        self.assertIsNone(m.seek_point(times, 0))
        self.assertTrue(times[23] < m.seek_point(times, 24) < times[24])

    def test_time_parsing_and_sampling(self):
        self.assertEqual(m.parse_time('1:02.5'), 62.5)
        self.assertEqual(m.parse_time('3'), 3.0)
        self.assertEqual(m.sample_times(0, 1, fps=4), [0, 0.25, 0.5, 0.75])
        self.assertEqual(m.sample_times(0, 2, count=2), [0.5, 1.5])
        with self.assertRaises(m.MediaError):
            m.sample_times(2, 1, fps=4)

    def test_ffmpeg_log_parsers(self):
        stderr = ('[Parsed_showinfo_1 @ 0x1] config in time_base: 1/1000\n'
                  '[Parsed_showinfo_1 @ 0x1] n:   0 pts:   1000 pts_time:1       duration: 41\n'
                  '[Parsed_showinfo_1 @ 0x1] n:   1 pts:   2542 pts_time:2.542   duration: 41\n')
        self.assertEqual(m.parse_showinfo(stderr), [1.0, 2.542])
        events = ('[blackdetect @ 0x2] black_start:1 black_end:1.5 black_duration:0.5\n'
                  '[freezedetect @ 0x3] lavfi.freezedetect.freeze_start: 2\n'
                  '[freezedetect @ 0x3] lavfi.freezedetect.freeze_duration: 1.2\n'
                  '[freezedetect @ 0x3] lavfi.freezedetect.freeze_end: 3.2\n'
                  '[freezedetect @ 0x3] lavfi.freezedetect.freeze_start: 5\n')
        black, freeze = m.parse_black_freeze(events)
        self.assertEqual(black, [{'start': 1.0, 'end': 1.5, 'duration': 0.5}])
        self.assertEqual(freeze, [{'start': 2.0, 'end': 3.2}, {'start': 5.0, 'end': None}])

    def test_packet_comparison(self):
        text = ('#tb 0: 1/1000\n#stream#, dts, pts, duration, size, hash\n'
                '0, -21, -21, 21, 390, aaa, S=1, 10, side\n0, 0, 0, 21, 350, bbb\n0, 21, 21, 21, 177, ccc\n')
        timebase, packets = m.parse_framemd5(text)
        self.assertEqual(timebase, Fraction(1, 1000))
        self.assertEqual(packets[0], (-21, 21, 390, 'aaa'))
        reference = (timebase, packets)
        status, detail = m.compare_packets(reference, reference, 0.0, 0.0, 1 / 24)
        self.assertEqual((status, detail['identical_payload']), ('pass', True))
        changed = (timebase, [packets[0], (0, 21, 350, 'xxx'), packets[2]])
        status, detail = m.compare_packets(reference, changed, 0.0, 0.0, 1 / 24)
        self.assertEqual((status, detail['first_mismatch']), ('fail', 1))
        self.assertNotIn('hint', detail)
        # Same payload, video now starts 21 ms earlier relative to audio: sub-frame shift needs review.
        status, detail = m.compare_packets(reference, reference, 0.021, 0.0, 1 / 24)
        self.assertEqual(status, 'review')
        self.assertAlmostEqual(detail['sync_offset_delta_seconds'], 0.021)
        self.assertIn('-itsoffset 0.021000 before the rendered video input', detail['hint'])
        status, detail = m.compare_packets(reference, reference, 0.0, 0.1, 1 / 24)
        self.assertEqual(status, 'fail')
        self.assertIn('-itsoffset 0.100000 before the reference audio input', detail['hint'])

    def test_cut_matching_and_shot_stats(self):
        matched, missing, extra = m.match_cuts([1.0, 2.0], [1.04, 3.0], 1 / 24)
        self.assertEqual([item['candidate'] for item in matched], [1.04])
        self.assertEqual((missing, extra), ([2.0], [3.0]))
        stats = m.shot_stats([1.0, 2.5], 4.0)
        self.assertEqual((stats['count'], stats['lengths'], stats['longest']), (3, [1.0, 1.5, 1.5], 1.5))

    def test_grid_layout(self):
        tiles = [(Image.new('RGB', (40, 20), 'red'), f'{index}') for index in range(5)]
        sheet = m.labeled_grid(tiles, 3, 'title')
        self.assertGreater(sheet.width, 3 * 40)
        self.assertGreater(sheet.height, 2 * 20)

    def test_copy_check_requires_reference_before_running_tools(self):
        with patch.object(sys, 'argv', ['media.py', 'verify', 'x.mkv', '--expect-audio', 'copy']), \
                patch.object(m, 'probe') as probe, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                m.main()
        self.assertEqual(caught.exception.code, 1)
        probe.assert_not_called()


@unittest.skipUnless(HAS_FFMPEG, 'ffmpeg/ffprobe not installed')
class MediaIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        scratch = Path(__file__).resolve().parents[3] / 'tmp'
        scratch.mkdir(exist_ok=True)
        cls.temp = tempfile.TemporaryDirectory(dir=scratch)
        cls.dir = Path(cls.temp.name)
        cls.ref, cls.silent = cls.dir / 'ref.mkv', cls.dir / '成片-silent.mkv'
        cls.final, cls.reencoded, cls.shifted = (cls.dir / name for name in ('final.mkv', 'reencoded.mkv', 'shifted.mkv'))
        # Animated source with a hard cut at 1.0 s to an inverted copy. FLAC has no encoder priming, so
        # every ffmpeg version muxes this reference with video and audio both starting at zero.
        cls.ffmpeg('-f', 'lavfi', '-i', 'testsrc2=s=160x90:r=24:d=1', '-f', 'lavfi', '-i', 'testsrc2=s=160x90:r=24:d=1,negate',
                   '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=2',
                   '-filter_complex', '[0:v][1:v]concat=n=2:v=1:a=0[v]', '-map', '[v]', '-map', '2:a',
                   '-c:v', 'ffv1', '-c:a', 'flac', cls.ref)
        cls.ffmpeg('-i', cls.ref, '-map', '0:v', '-c:v', 'mpeg4', '-q:v', '4', cls.silent)
        # The documented replica mux: silent render video plus stream-copied reference audio.
        cls.mux(cls.silent, cls.ref, cls.final)
        cls.ffmpeg('-i', cls.silent, '-i', cls.ref, '-map', '0:v:0', '-map', '1:a', '-c:v', 'copy',
                   '-c:a', 'aac', '-b:a', '64k', cls.reencoded)
        cls.mux(cls.silent, cls.ref, cls.shifted, audio_offset=0.1)
        # References whose video starts after the audio: AAC priming (how much depends on the ffmpeg
        # version's muxer) and an explicit late video start.
        cls.offset_cases = {}
        for name, video_input, audio in (
                ('aac', ['-f', 'lavfi', '-i', 'testsrc2=s=160x90:r=24:d=2'], ['-c:a', 'aac', '-b:a', '96k']),
                ('late', ['-itsoffset', '0.05', '-f', 'lavfi', '-i', 'testsrc2=s=160x90:r=24:d=2'], ['-c:a', 'flac'])):
            ref, silent, final = (cls.dir / f'{name}-{kind}.mkv' for kind in ('ref', 'silent', 'final'))
            cls.ffmpeg(*video_input, '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=2',
                       '-map', '0:v', '-map', '1:a', '-c:v', 'ffv1', *audio, ref)
            cls.ffmpeg('-i', ref, '-map', '0:v', '-c:v', 'mpeg4', '-q:v', '4', silent)
            cls.mux(silent, ref, final)
            cls.offset_cases[name] = (ref, silent, final)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    @staticmethod
    def ffmpeg(*args):
        subprocess.run([shutil.which('ffmpeg'), '-hide_banner', '-v', 'error', '-y', *map(str, args)], check=True)

    @classmethod
    def mux(cls, video, reference, output, video_offset=0.0, audio_offset=0.0):
        # Stream-copy mux; an -itsoffset before an input delays that input's timestamps.
        cls.ffmpeg(*(['-itsoffset', f'{video_offset:.6f}'] if video_offset else []), '-i', video,
                   *(['-itsoffset', f'{audio_offset:.6f}'] if audio_offset else []), '-i', reference,
                   '-map', '0:v:0', '-map', '1:a?', '-c', 'copy', output)

    @staticmethod
    def audio(result):
        return next(item for item in result['checks'] if item['name'].startswith('audio'))

    def test_midpoint_seek_returns_the_exact_frame(self):
        info = m.probe(self.ref)
        stream, times = m.video_stream(info), m.frame_times(self.ref, info)
        self.assertEqual(len(times), 48)
        extracted = m.frame_image(self.ref, stream, times, 24, 160)
        decoded = subprocess.run([shutil.which('ffmpeg'), '-hide_banner', '-v', 'error', '-i', str(self.ref),
                                  '-map', '0:0', '-vf', "select='eq(n,24)',scale=160:-2:flags=lanczos",
                                  '-frames:v', '1', '-f', 'image2pipe', '-c:v', 'png', '-'],
                                 capture_output=True, check=True).stdout
        with Image.open(io.BytesIO(decoded)) as image:
            self.assertIsNone(ImageChops.difference(extracted, image.convert('RGB')).getbbox())

    def test_cut_detection_and_reference_alignment(self):
        result = m.cuts(self.final, reference=self.ref)
        self.assertEqual(len(result['cuts']), 1)
        self.assertAlmostEqual(result['cuts'][0]['time'], 1.0, delta=0.05)
        self.assertEqual(result['cuts'][0]['frame'], 24)
        self.assertEqual(result['reference']['status'], 'aligned')

    def test_audio_copy_verified_packet_by_packet(self):
        result = m.verify(self.final, self.ref, 'copy')
        audio = self.audio(result)
        self.assertEqual(audio['status'], 'pass')
        self.assertTrue(audio['detail']['streams'][0]['identical_payload'])
        self.assertEqual({item['name']: item['status'] for item in result['checks']}['decode'], 'pass')
        self.assertEqual(result['layer'], 'engineering')

    def test_reencoded_or_shifted_audio_fails(self):
        audio = self.audio(m.verify(self.reencoded, self.ref, 'copy'))
        self.assertEqual(audio['status'], 'fail')
        self.assertFalse(audio['detail']['streams'][0]['identical_payload'])
        audio = self.audio(m.verify(self.shifted, self.ref, 'copy'))
        self.assertEqual(audio['status'], 'fail')
        self.assertAlmostEqual(audio['detail']['streams'][0]['sync_offset_delta_seconds'], 0.1, delta=0.002)

    def test_start_offsets_are_reported_and_correctable(self):
        for name, (ref, silent, final) in self.offset_cases.items():
            with self.subTest(case=name):
                stream = self.audio(m.verify(final, ref, 'copy'))['detail']['streams'][0]
                self.assertTrue(stream['identical_payload'])
                shift = stream['sync_offset_delta_seconds']
                if name == 'late':
                    self.assertGreater(shift, 0.03)
                if abs(shift) > m.TIMING_TOLERANCE:
                    self.assertIn('-itsoffset', stream['hint'])
                # Apply the documented correction and verify again.
                fixed = self.dir / f'{name}-fixed.mkv'
                self.mux(silent, ref, fixed, video_offset=max(shift, 0), audio_offset=max(-shift, 0))
                self.assertEqual(self.audio(m.verify(fixed, ref, 'copy'))['status'], 'pass')

    def test_expected_silence(self):
        self.assertEqual(self.audio(m.verify(self.silent, expect_audio='none'))['status'], 'pass')
        self.assertEqual(self.audio(m.verify(self.final, expect_audio='none'))['status'], 'fail')

    def test_sheet_and_compare_evidence(self):
        output = self.dir / 'evidence/sheet-v1.png'
        result = m.sheet(self.ref, output, start=0.8, end=1.2, every_frame=True, width=120, columns=5)
        self.assertEqual([frame['index'] for frame in result['frames']], list(range(20, 29)))
        with Image.open(output) as image:
            self.assertGreater(image.width, 5 * 120)
        with self.assertRaises(m.MediaError):
            m.sheet(self.ref, output, count=4)
        result = m.compare(self.ref, self.final, self.dir / 'evidence/compare-v1.png', times=[0.5, 1.5], width=120, diff=True)
        self.assertEqual([(row['reference_frame'], row['candidate_frame']) for row in result['rows']], [(12, 12), (36, 36)])
        self.assertEqual(result['warnings'], [])


if __name__ == '__main__':
    unittest.main()
