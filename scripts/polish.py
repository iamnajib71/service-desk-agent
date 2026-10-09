"""Build published views from retained real evidence; Pillow is optional dev tooling."""
import argparse
import json
from pathlib import Path
import shutil


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("results", help="Full 20-scenario results directory")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    folder = Path(args.results)
    rows = json.loads((folder / "summary.json").read_text())
    manifest = json.loads((folder / "manifest.json").read_text())
    scenarios = []
    for row in rows:
        audit = json.loads((folder / (row["id"] + "-agent.json")).read_text())
        approvals = {json.loads(item["data"])["key"]: json.loads(item["data"])["reviewer"]
                     for item in audit["audit"] if item["event"] == "approval"}
        steps = []
        for step in audit["steps"]:
            decision = json.loads(step["decision"])
            steps.append({"action": decision["action"], "reflection": decision["reflection"],
                          "approval": approvals.get(step["key"]),
                          "result": json.loads(step["result"]) if step["result"] else {"state": step["state"]}})
        scenarios.append({"id": row["id"], "name": row["name"], "result": row,
                          "ticket": audit["run"]["ticket"] if audit["run"] else "Fixture setup failed",
                          "final": audit["run"]["final"] if audit["run"] else "See raw fixture logs", "steps": steps})
    (root / "docs/site/data.json").write_text(json.dumps({"model": manifest["model"], "scenarios": scenarios}, indent=2), encoding="utf-8")
    if (root / "docs/demo.gif").exists():
        shutil.copyfile(root / "docs/demo.gif", root / "docs/site/demo.gif")
    (root / "eval/results/latest.json").write_text(json.dumps({"directory": folder.name}, indent=2), encoding="utf-8")
    make_social(root)


def make_social(root):
    from PIL import Image, ImageDraw, ImageFont
    image = Image.new("RGB", (1280, 640), "#09111c")
    draw = ImageDraw.Draw(image)
    font_path = Path("C:/Windows/Fonts/segoeui.ttf")
    bold_path = Path("C:/Windows/Fonts/segoeuib.ttf")
    def font(size, bold=False):
        return ImageFont.truetype(str(bold_path if bold else font_path), size)
    mint, text, muted = "#67ebc0", "#e6eef7", "#9aafc5"
    draw.rounded_rectangle((52, 44, 286, 85), radius=9, fill="#173f36")
    draw.text((69, 49), "LOCAL AI / REAL IT", font=font(21, True), fill=mint)
    draw.text((52, 109), "Service Desk Agent", font=font(74, True), fill=text)
    draw.text((56, 210), "Human approval. Tool use. Memory. Recovery.", font=font(30), fill=muted)
    boxes = [(56, 312, 305, 420), (354, 312, 604, 420), (653, 312, 903, 420), (952, 312, 1224, 420)]
    for box, title, subtitle in zip(boxes, ["TICKET", "LOCAL MODEL", "BOUNDED TOOLS", "VERIFIED STATE"],
                                 ["Fictional homelab staff", "Ollama + embeddings", "AD / SMB / CUPS / VPN", "Audit + checkpoints"]):
        draw.rounded_rectangle(box, radius=14, fill="#111e2d", outline="#263b51", width=2)
        draw.text((box[0]+18, box[1]+18), title, font=font(21, True), fill=mint)
        draw.text((box[0]+18, box[1]+58), subtitle, font=font(18), fill=text)
    for x in (321, 620, 919):
        draw.line((x-6, 366, x+20, 366), fill=mint, width=3)
        draw.polygon([(x+20,366),(x+11,360),(x+11,372)], fill=mint)
    draw.line((56, 481, 1224, 481), fill="#263b51", width=2)
    draw.text((56, 516), "Built by Nazmul Hassan", font=font(27, True), fill=text)
    draw.text((56, 563), "github.com/iamnajib71/service-desk-agent", font=font(23), fill=muted)
    image.save(root / "docs/social-preview.png")
    # Capture actual agg-rendered terminal frames, without inventing terminal output.
    gif = Image.open(root / "docs/demo.gif")
    target = root / "docs/img"
    target.mkdir(exist_ok=True)
    for name, fraction in (("approval", .38), ("recovery", .98)):
        gif.seek(min(gif.n_frames-1, int(gif.n_frames*fraction)))
        gif.convert("RGB").save(target / (name + ".png"))


if __name__ == "__main__":
    main()

