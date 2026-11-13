#!/usr/bin/env python3
"""Full browser regression suite for ABYSSAL REDO.

The test-only API is injected into an in-memory copy of index.html. It is NOT
written into the deployed game, so private game functions stay private in production.
"""
from pathlib import Path
import hashlib
import json
import shutil
import sys
import time
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "index.html"
REPORT = {"checks": [], "errors": [], "metrics": {}}
SHOTS = ROOT / "biome-screenshots"

def record(name, ok, detail=""):
    REPORT["checks"].append({"name": name, "status": "PASS" if ok else "FAIL", "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        REPORT["errors"].append(name)

TEST_API = r"""
      window.__ABYSSAL_TEST__ = {
        boot() {
          newRun(); state='playing'; fade=0; banner.t=0; render();
          return {state, floorN, roomW, roomH, level, player:!!P, canvas:!!ctx};
        },
        floor(floor, frames=90) {
          floorN=floor; roomN=1; cell={type:'combat',cleared:true,looted:false};
          startRoom(); state='playing'; fade=0; banner.t=0; stateT=5;
          for(let i=0;i<frames;i++){updatePlaying(1/60);updateRoomCamera(1/60);}
          fade=0; render();
          return {floor:floorN,width:roomW,height:roomH,enemies:enemies.length,state,biome:themeForFloor().biome,name:themeForFloor().name};
        },
        bossKill(floor, mode) {
          floorN=floor; roomN=4; cell={type:'boss',cleared:false,looted:false};
          startRoom(); state='playing'; fade=0; banner.t=0;
          const boss=enemies.find(e=>e.boss);
          if(!boss) return {ok:false,reason:'boss did not spawn'};
          boss.spawnT=0;boss.orbit=0;boss.hp=1;boss.maxHp=Math.max(1,boss.maxHp);
          boss.phaseInvuln=0;boss.despLock=false;boss.despFinished=true;boss.ecd=999;
          if(mode==='normal projectile'){
            bullets.push({type:'acid',x:boss.x,y:boss.y,vx:0,vy:0,r:30,life:2,dmg:1e9,a:0});
            updateBullets(1/60);
          }else{
            ebullets.push({x:boss.x,y:boss.y,vx:0,vy:0,r:30,life:2,dmg:1e9,col:'#ffe94a',fr:true});
            updateEBullets(1/60);
          }
          render();
          return {ok:boss.dead===true&&bullets.length===0&&ebullets.length===0,dead:boss.dead,bullets:bullets.length,ebullets:ebullets.length,state,floor:floorN};
        },
        progression() {
          floorN=8;newRun();level=MAX_LEVEL-1;xp=0;addXP(1e9);
          const capped=level===MAX_LEVEL;
          state='paused';render();const pauseRendered=state==='paused';
          state='gameover';P.dead=true;render();const gameOverRendered=state==='gameover';
          state='playing';P.dead=false;fade=0;render();
          return {maxLevel:MAX_LEVEL,level,capped,pauseRendered,gameOverRendered};
        },
        stress() {
          floorN=8;roomN=1;cell={type:'combat',cleared:true,looted:false};startRoom();state='playing';fade=0;
          for(let i=0;i<120;i++){
            const x=roomW/2+(Math.random()-.5)*500,y=roomH/2+(Math.random()-.5)*300;
            ebullets.push({x,y,vx:(Math.random()-.5)*80,vy:(Math.random()-.5)*80,r:7,life:2,dmg:1,col:'#ff4fd8'});
            bullets.push({type:'acid',x,y,vx:20,vy:10,r:6,life:2,dmg:1,a:0});
          }
          const t0=performance.now();
          for(let i=0;i<180;i++){updateBullets(1/60);updateEBullets(1/60);updateParts(1/60);render();}
          return {frames:180,elapsedMs:performance.now()-t0,bullets:bullets.length,enemyBullets:ebullets.length,particles:parts.length};
        }
      };
"""

def main():
    if not HTML.exists():
        raise SystemExit(f"Missing game file: {HTML}")
    original = HTML.read_text(encoding="utf-8")
    marker = "    })();\n  </script>"
    injection_point = original.rfind(marker)
    if injection_point < 0:
        raise SystemExit("Could not safely find the end of the game IIFE for isolated tests.")
    instrumented = original[:injection_point] + TEST_API + "\n" + original[injection_point:]
    # Shim secure-context-only UUID API before the game's first script executes.
    uuid_shim = "<script>Object.defineProperty(window.crypto,'randomUUID',{configurable:true,value:(()=>{let id=0;return()=>String(++id)+'-test-uuid'})()});</script>"
    instrumented = instrumented.replace("<script>", uuid_shim + "\\n  <script>", 1)
    if SHOTS.exists():
        shutil.rmtree(SHOTS)
    SHOTS.mkdir(parents=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_page(viewport={"width":1280,"height":720},device_scale_factor=1)
        page_errors,console_errors=[],[]
        page.on("pageerror",lambda e:page_errors.append(str(e)))
        page.on("console",lambda m:console_errors.append(m.text) if m.type=="error" else None)
        page.set_content(instrumented,wait_until="load")
        page.wait_for_selector("#game")
        record("HTML loads and canvas exists",page.locator("#game").count()==1,page.title())

        if page.evaluate("() => !window.__ABYSSAL_TEST__"):
            raise RuntimeError("Test API was not installed. page errors=" + json.dumps(page_errors) + " console=" + json.dumps(console_errors))
        boot=page.evaluate("() => window.__ABYSSAL_TEST__.boot()")
        record("New run initializes playable state",boot["state"]=="playing" and boot["player"] and boot["canvas"],json.dumps(boot))

        dimensions,biomes,screenshot_hashes=[],[],[]
        for floor in range(1,9):
            result=page.evaluate("(floor)=>window.__ABYSSAL_TEST__.floor(floor,90)",floor)
            dimensions.append([result["width"],result["height"]])
            biomes.append(result["biome"])
            shot=SHOTS/f"abyss-{floor}-{result['biome']}.png"
            page.locator("#game").screenshot(path=str(shot))
            screenshot_hashes.append(hashlib.sha256(shot.read_bytes()).hexdigest())
            record(f"Abismo {floor}: generate, simulate and render",result["state"]=="playing" and result["width"]>=1280 and result["height"]>=720,
                   f"{result['name']} · {result['width']}x{result['height']} · enemies={result['enemies']}")
        increasing=all(dimensions[i][0]<=dimensions[i+1][0] and dimensions[i][1]<=dimensions[i+1][1] for i in range(7))
        record("Room dimensions do not shrink with depth",increasing,str(dimensions))
        record("Eight different biome identities",len(set(biomes))==8,str(biomes))
        distinct_shots=len(set(screenshot_hashes))
        record("Eight distinct rendered biome captures",distinct_shots==8,f"{distinct_shots}/8 unique screenshots")
        REPORT["metrics"]["biomes"]={"names":biomes,"dimensions":dimensions,"uniqueScreenshotCount":distinct_shots}

        for floor in range(1,8):
            for mode in ("normal projectile","reflected parry projectile"):
                outcome=page.evaluate("({floor,mode})=>window.__ABYSSAL_TEST__.bossKill(floor,mode)",{"floor":floor,"mode":mode})
                record(f"Boss {floor}: death via {mode} + cleanup + render",outcome.get("ok",False),json.dumps(outcome))

        progression=page.evaluate("()=>window.__ABYSSAL_TEST__.progression()")
        record("Level cap and pause/game-over render paths",
               progression["capped"] and progression["pauseRendered"] and progression["gameOverRendered"],json.dumps(progression))

        stress=page.evaluate("()=>window.__ABYSSAL_TEST__.stress()")
        REPORT["metrics"]["stress"]=stress
        record("Stress test: 180 frames under projectile load",stress["elapsedMs"]<15000,json.dumps(stress))
        record("No uncaught browser JavaScript exceptions",not page_errors,json.dumps(page_errors[:8]))
        record("No browser console errors",not console_errors,json.dumps(console_errors[:8]))
        browser.close()

    out=ROOT/"abyssal-simulation-report.json"
    out.write_text(json.dumps(REPORT,indent=2,ensure_ascii=False),encoding="utf-8")
    print("\nSUMMARY:",json.dumps({"passed":sum(x["status"]=="PASS" for x in REPORT["checks"]),
                                  "failed":sum(x["status"]=="FAIL" for x in REPORT["checks"]),
                                  "report":str(out),"screenshots":str(SHOTS)},ensure_ascii=False))
    return 1 if REPORT["errors"] else 0

if __name__=="__main__":
    sys.exit(main())
