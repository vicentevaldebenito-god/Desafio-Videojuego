#!/usr/bin/env python3
"""Browser-based regression simulation for ABYSSAL REDO.

This suite exercises the real index.html in headless Chromium. It is intentionally
non-invasive: all game state changes happen in an isolated browser session.
"""
from pathlib import Path
import json
import sys
import time
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "index.html"
REPORT = {"checks": [], "errors": [], "metrics": {}}

def record(name, ok, detail=""):
    REPORT["checks"].append({"name": name, "status": "PASS" if ok else "FAIL", "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        REPORT["errors"].append(name)

def main():
    if not HTML.exists():
        raise SystemExit(f"Missing game file: {HTML}")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_page(viewport={"width": 1280, "height": 720}, device_scale_factor=1)
        page_errors, console_errors = [], []
        page.on("pageerror", lambda e: page_errors.append(str(e)))
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
        page.goto(HTML.as_uri(), wait_until="load")
        page.wait_for_selector("#game")
        record("HTML loads and canvas exists", page.locator("#game").count() == 1, page.title())

        boot = page.evaluate("""() => {
          newRun();
          state = 'playing';
          render();
          return {state, floorN, roomW, roomH, level, player: !!P, canvas: !!ctx};
        }""")
        record("New run initializes playable state", boot["state"] == "playing" and boot["player"] and boot["canvas"], json.dumps(boot))

        # Exercise all eight depth bands, room generation, update and rendering.
        dimensions = []
        for floor in range(1, 9):
            result = page.evaluate("""floor => {
              floorN = floor; roomN = 1;
              cell = {type:'combat', cleared:false, looted:false};
              startRoom(); state = 'playing';
              for (let i=0;i<90;i++) updatePlaying(1/60);
              render();
              return {floor:floorN, width:roomW, height:roomH, enemies:enemies.length, state};
            }""", floor)
            dimensions.append([result["width"], result["height"]])
            record(f"Abismo {floor}: generate, simulate 90 frames and render",
                   result["state"] == "playing" and result["width"] >= 1280 and result["height"] >= 720,
                   f"{result['width']}x{result['height']}, enemies={result['enemies']}")
        increasing = all(dimensions[i][0] <= dimensions[i+1][0] and dimensions[i][1] <= dimensions[i+1][1] for i in range(7))
        record("Room dimensions do not shrink with depth", increasing, str(dimensions))

        # Exercise all seven conventional boss rooms, killing each via a normal projectile
        # and a reflected/parry projectile. This deliberately hits the exact mutation path
        # that previously caused the Echo death screen to break.
        for floor in range(1, 8):
            for mode in ("normal projectile", "reflected parry projectile"):
                outcome = page.evaluate("""({floor, mode}) => {
                  floorN=floor; roomN=4;
                  cell={type:'boss',cleared:false,looted:false};
                  startRoom(); state='playing';
                  const boss=enemies.find(e=>e.boss);
                  if(!boss) return {ok:false, reason:'boss did not spawn'};
                  boss.spawnT=0; boss.orbit=0; boss.hp=1; boss.maxHp=Math.max(1,boss.maxHp);
                  boss.phaseInvuln=0; boss.despLock=false; boss.despFinished=true; boss.ecd=999;
                  if(mode==='normal projectile') {
                    bullets.push({type:'acid',x:boss.x,y:boss.y,vx:0,vy:0,r:30,life:2,dmg:1e9,a:0});
                    updateBullets(1/60);
                  } else {
                    ebullets.push({x:boss.x,y:boss.y,vx:0,vy:0,r:30,life:2,dmg:1e9,col:'#ffe94a',fr:true});
                    updateEBullets(1/60);
                  }
                  render();
                  return {ok:boss.dead===true && bullets.length===0 && ebullets.length===0,
                          dead:boss.dead, bullets:bullets.length, ebullets:ebullets.length,
                          state, floor:floorN};
                }""", {"floor": floor, "mode": mode})
                record(f"Jefe {floor}: muerte con {mode} + limpieza + render",
                       outcome.get("ok", False), json.dumps(outcome))

        # Check progression cap and state transitions.
        progression = page.evaluate("""() => {
          floorN=8; newRun(); level=MAX_LEVEL-1; xp=0; addXP(1e9);
          const capped=level===MAX_LEVEL;
          state='paused'; render(); const pauseRendered=state==='paused';
          state='gameover'; P.dead=true; render(); const gameOverRendered=state==='gameover';
          state='playing'; P.dead=false; render();
          return {maxLevel:MAX_LEVEL, level, capped, pauseRendered, gameOverRendered};
        }""")
        record("Level cap and pause/game-over render paths", progression["capped"] and progression["pauseRendered"] and progression["gameOverRendered"], json.dumps(progression))

        # Stress a bounded number of frames with many projectiles and particles.
        stress = page.evaluate("""() => {
          floorN=8; roomN=1; cell={type:'combat',cleared:false,looted:false}; startRoom(); state='playing';
          for(let i=0;i<120;i++){
            const x=roomW/2+(Math.random()-.5)*500, y=roomH/2+(Math.random()-.5)*300;
            ebullets.push({x,y,vx:(Math.random()-.5)*80,vy:(Math.random()-.5)*80,r:7,life:2,dmg:1,col:'#ff4fd8'});
            bullets.push({type:'acid',x,y,vx:20,vy:10,r:6,life:2,dmg:1,a:0});
          }
          const t0=performance.now();
          for(let i=0;i<180;i++){updateBullets(1/60);updateEBullets(1/60);updateParts(1/60);render();}
          return {frames:180, elapsedMs:performance.now()-t0, bullets:bullets.length, enemyBullets:ebullets.length, particles:parts.length};
        }""")
        REPORT["metrics"]["stress"] = stress
        record("Stress test: 180 frames with projectile load", stress["elapsedMs"] < 15000, json.dumps(stress))

        # Verify that no browser runtime exception was thrown at any point.
        record("No uncaught browser JavaScript exceptions", not page_errors, json.dumps(page_errors[:8]))
        record("No browser console errors", not console_errors, json.dumps(console_errors[:8]))
        browser.close()

    out = ROOT / "abyssal-simulation-report.json"
    out.write_text(json.dumps(REPORT, indent=2, ensure_ascii=False), encoding="utf-8")
    print("\nSUMMARY:", json.dumps({"passed":sum(x["status"]=="PASS" for x in REPORT["checks"]),
                                  "failed":sum(x["status"]=="FAIL" for x in REPORT["checks"]),
                                  "report":str(out)}, ensure_ascii=False))
    return 1 if REPORT["errors"] else 0

if __name__ == "__main__":
    sys.exit(main())
