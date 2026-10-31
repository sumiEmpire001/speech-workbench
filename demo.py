import argparse
import io
import json
import tempfile
import wave
from pathlib import Path
from speech import Workbench, from_srt

parser=argparse.ArgumentParser(description="Provider-free synthetic audio/job/subtitle demo")
parser.add_argument("--output-dir",type=Path)
args=parser.parse_args()
buffer=io.BytesIO()
with wave.open(buffer,"wb") as audio:
    audio.setnchannels(1);audio.setsampwidth(2);audio.setframerate(8000)
    audio.writeframes(b"\x00\x00"*8000)
with tempfile.TemporaryDirectory() as folder:
    workbench=Workbench(Path(folder)/"speech.db")
    audio=workbench.ingest("synthetic-silence.wav",buffer.getvalue())
    job=workbench.enqueue(audio,"synthetic-fixture","demo-event")
    def fixture(data,duration):
        return [{"start":0,"end":500,"text":"[Synthetic caption fixture]"}]
    workbench.process_one({"synthetic-fixture":fixture})
    edited=[{"start":100,"end":700,"text":"[Human-reviewed synthetic fixture]"}]
    workbench.edit(job,edited,expected_revision=0,actor="demo-reviewer",reason="Synthetic subtitle timing review")
    srt=workbench.export(job)
    assert from_srt(srt,1000)==edited
    print("Synthetic silence and fixture captions — no speech recognition performed")
    print(srt)
    if args.output_dir:
        args.output_dir.mkdir(parents=True,exist_ok=True)
        (args.output_dir/"captions.srt").write_text(srt,encoding="utf-8")
        (args.output_dir/"captions.vtt").write_text(workbench.export(job,format="vtt"),encoding="utf-8")
        (args.output_dir/"transcript.json").write_text(json.dumps(workbench.get(job),indent=2),encoding="utf-8")
        print("Synthetic subtitle artifacts:",args.output_dir)
