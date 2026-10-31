import io
import tempfile
import unittest
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from speech import Workbench, Conflict, to_srt, from_srt, validate_segments


SEGMENTS=[{"start":0,"end":400,"text":"Synthetic café 🎯"},{"start":500,"end":1000,"text":"Second line\nContinued"}]


def audio():
    output=io.BytesIO()
    with wave.open(output,"wb") as stream:
        stream.setnchannels(1);stream.setsampwidth(2);stream.setframerate(8000);stream.writeframes(b"\x00\x00"*8000)
    return output.getvalue()


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.now=1000;self.path=Path(self.temp.name)/"speech.db"
        self.work=Workbench(self.path,lambda:self.now)
        self.audio=self.work.ingest("synthetic.wav",audio())
        self.job=self.work.enqueue(self.audio,"fixture","event")

    def ready(self):
        return self.work.process_one({"fixture":lambda data,duration:SEGMENTS})

    def test_ingestion_metadata_and_event_deduplication(self):
        self.assertEqual(self.work.ingest("renamed.wav",audio()),self.audio)
        self.assertEqual(self.work.enqueue(self.audio,"fixture","event"),self.job)
        self.assertEqual(self.work.get(self.job)["duration"],1000)
        with self.assertRaises(Conflict):
            self.work.enqueue(self.audio,"other","event")

    def test_invalid_truncated_and_oversized_wav(self):
        for data in (b"garbage",audio()[:-100],b"x"*(10*1024*1024+1)):
            with self.assertRaises(ValueError):
                self.work.ingest("bad.wav",data)

    def test_ready_transcript_and_provider_receives_real_wav(self):
        def fixture(data,duration):
            self.assertTrue(data.startswith(b"RIFF"));self.assertEqual(duration,1000)
            return SEGMENTS
        result=self.work.process_one({"fixture":fixture})
        self.assertEqual(result["state"],"ready")
        self.assertEqual(result["original"],SEGMENTS)
        self.assertIsNone(self.work.claim())

    def test_invalid_segment_order_boundaries_and_text(self):
        invalid=[[{"start":0,"end":1001,"text":"long"}],[{"start":True,"end":100,"text":"bool"}],
                 [{"start":100,"end":100,"text":"empty"}],[{"start":0,"end":500,"text":"x"},{"start":400,"end":600,"text":"overlap"}],
                 [{"start":0,"end":100,"text":"line\n\nline"}],[{"start":0,"end":100,"text":""}]]
        for segments in invalid:
            with self.assertRaises(ValueError):
                validate_segments(segments,1000)
        self.assertEqual(validate_segments([],1000),[])

    def test_srt_unicode_multiline_crlf_roundtrip(self):
        exported=to_srt(SEGMENTS,1000)
        self.assertEqual(from_srt(exported,1000),SEGMENTS)
        self.assertEqual(from_srt("\ufeff"+exported.replace("\n","\r\n"),1000),SEGMENTS)
        self.assertEqual(from_srt(to_srt([],1000),1000),[])

    def test_invalid_srt_sequence_timestamps_and_overlap(self):
        invalid=["2\n00:00:00,000 --> 00:00:00,500\nx\n","1\n00:60:00,000 --> 00:61:00,000\nx\n","1\n00:00:00.000 --> 00:00:00.500\nx\n",to_srt(SEGMENTS,1000).replace("00:00:00,500","00:00:00,300")]
        for value in invalid:
            with self.assertRaises(ValueError):
                from_srt(value,1000)

    def test_edit_preserves_original_and_audit(self):
        self.ready()
        changed=[{"start":50,"end":600,"text":"Corrected by reviewer"}]
        self.work.edit(self.job,changed,expected_revision=0,actor="reviewer",reason="Synthetic correction")
        result=self.work.get(self.job)
        self.assertEqual(result["original"],SEGMENTS)
        self.assertEqual(result["segments"],changed)
        self.assertEqual(result["revision"],1)
        self.assertEqual(len(self.work.history(self.job)),1)
        with self.assertRaises(Conflict):
            self.work.edit(self.job,SEGMENTS,expected_revision=0,actor="other",reason="Stale")

    def test_concurrent_claim_and_review_exclusion(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            claims=list(pool.map(lambda _:self.work.claim(),range(16)))
        self.assertEqual(sum(row is not None for row in claims),1)
        row=next(row for row in claims if row is not None)
        self.work.settle(self.job,row["token"],segments=SEGMENTS)
        def review(_):
            try:
                self.work.edit(self.job,[],expected_revision=0,actor="reviewer",reason="Test")
                return True
            except Conflict:
                return False
        with ThreadPoolExecutor(max_workers=6) as pool:
            self.assertEqual(sum(pool.map(review,range(12))),1)

    def test_expired_lease_restart_and_stale_worker(self):
        first=self.work.claim(lease=1);self.now+=1
        self.work=Workbench(self.path,lambda:self.now)
        second=self.work.claim()
        with self.assertRaises(Conflict):
            self.work.settle(self.job,first["token"],segments=SEGMENTS)
        self.work.settle(self.job,second["token"],segments=SEGMENTS)
        self.assertEqual(self.work.get(self.job)["attempts"],2)

    def test_failures_backoff_dead_letters_and_redaction(self):
        def failed(data,duration):
            raise RuntimeError("secret provider response")
        for attempt in range(1,4):
            result=self.work.process_one({"fixture":failed})
            self.assertEqual(result["error"],"RuntimeError")
            self.assertEqual(result["state"],"dead" if attempt==3 else "pending")
            self.assertIsNone(self.work.claim())
            self.now=result["due"]
        with self.assertRaises(Conflict):
            self.work.export(self.job)

    def test_invalid_adapter_output_retries_without_publishing(self):
        result=self.work.process_one({"fixture":lambda data,duration:[{"start":900,"end":2000,"text":"bad"}]})
        self.assertEqual(result["state"],"pending")
        self.assertIsNone(result["segments"])
        self.assertEqual(result["error"],"ValueError")

    def test_audio_integrity_and_crash_attempt_limit(self):
        with self.work.db(True) as db:
            db.execute("UPDATE audio SET data=? WHERE id=?",(b"tampered",self.audio))
        with self.assertRaises(ValueError):
            self.work.claim()
        with self.work.db(True) as db:
            db.execute("UPDATE audio SET data=? WHERE id=?",(audio(),self.audio))
        for _ in range(3):
            self.assertIsNotNone(self.work.claim(lease=1));self.now+=1
        self.assertIsNone(self.work.claim())
        self.assertEqual(self.work.get(self.job)["state"],"dead")

    def test_vtt_escapes_markup_and_survives_restart(self):
        self.ready()
        self.work.edit(self.job,[{"start":0,"end":500,"text":"<script>& -->"}],expected_revision=0,actor="reviewer",reason="Markup fixture")
        self.work=Workbench(self.path)
        vtt=self.work.export(self.job,format="vtt")
        self.assertTrue(vtt.startswith("WEBVTT\n\n"))
        self.assertIn("00:00:00.000 --> 00:00:00.500",vtt)
        self.assertIn("&lt;script&gt;&amp; --&gt;",vtt)
        self.assertNotIn("<script>",vtt)
        self.assertEqual(len(self.work.history(self.job)),1)


if __name__=="__main__":
    unittest.main()
