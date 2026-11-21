/**
 * record.js: Playwright automated screen recorder for Project Venessa.
 *
 * Requirements met:
 * - Chromium with viewport 1920x1080 and recordVideo size 1920x1080.
 * - Injected visible fake cursor (a glowing dot following mouse movements).
 * - Smooth mouse movements with steps: 25 before every click.
 * - Keyboard typing with 60ms delay.
 * - Pre-flight storageState login/session handling so startup is not recorded.
 * - Exact timing synchronization with audio durations + 0.5s pause.
 * - Logs start time of each step to timings.json.
 */

const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const BASE_DIR = __dirname;
const NARRATION_PATH = path.join(BASE_DIR, 'narration.json');
const DURATIONS_PATH = path.join(BASE_DIR, 'durations.json');
const TIMINGS_PATH = path.join(BASE_DIR, 'timings.json');
const AUTH_STATE_PATH = path.join(BASE_DIR, 'auth.json');
const RECORDINGS_DIR = path.join(BASE_DIR, 'recordings');

const APP_URL = process.env.APP_URL || 'http://localhost:5173';
const PAUSE_AFTER_STEP_MS = 500;

// Helper to sleep
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Injects a visual cursor dot into the browser DOM that tracks mouse coordinates.
 */
async function injectFakeCursor(page) {
  await page.evaluate(() => {
    const cursor = document.createElement('div');
    cursor.id = 'playwright-fake-cursor';
    cursor.style.position = 'fixed';
    cursor.style.top = '0';
    cursor.style.left = '0';
    cursor.style.width = '20px';
    cursor.style.height = '20px';
    cursor.style.borderRadius = '50%';
    cursor.style.backgroundColor = 'rgba(239, 68, 68, 0.9)'; // Vivid coral red
    cursor.style.border = '2px solid #ffffff';
    cursor.style.boxShadow = '0 0 12px rgba(239, 68, 68, 0.7)';
    cursor.style.pointerEvents = 'none';
    cursor.style.zIndex = '9999999';
    cursor.style.transform = 'translate(-50%, -50%)';
    cursor.style.transition = 'transform 0.08s ease, background-color 0.1s ease';
    document.body.appendChild(cursor);

    window.addEventListener('mousemove', (e) => {
      cursor.style.left = `${e.clientX}px`;
      cursor.style.top = `${e.clientY}px`;
    });

    window.addEventListener('mousedown', () => {
      cursor.style.transform = 'translate(-50%, -50%) scale(0.75)';
      cursor.style.backgroundColor = 'rgba(220, 38, 38, 1)';
    });

    window.addEventListener('mouseup', () => {
      cursor.style.transform = 'translate(-50%, -50%) scale(1)';
      cursor.style.backgroundColor = 'rgba(239, 68, 68, 0.9)';
    });
  });
}

/**
 * Moves mouse smoothly in 25 intermediate steps to a target bounding box or coordinate.
 */
async function smoothMove(page, target) {
  let x, y;
  if (typeof target === 'string') {
    const loc = page.locator(target).first();
    const visible = await loc.isVisible().catch(() => false);
    if (!visible) return null;
    const box = await loc.boundingBox();
    if (!box) return null;
    x = box.x + box.width / 2;
    y = box.y + box.height / 2;
  } else if (target && typeof target.x === 'number') {
    x = target.x;
    y = target.y;
  } else {
    return null;
  }
  await page.mouse.move(x, y, { steps: 25 });
  await sleep(60);
  return { x, y };
}

/**
 * Moves mouse smoothly and performs a click.
 */
async function smoothClick(page, target) {
  const coords = await smoothMove(page, target);
  if (coords) {
    await sleep(80);
    await page.mouse.click(coords.x, coords.y);
    await sleep(100);
  }
}

/**
 * Performs pre-flight initialization and saves storageState so login/init is not recorded.
 */
async function performPreflightSetup() {
  console.log('[Setup] Running pre-flight setup to initialize session state...');
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport: { width: 1920, height: 1080 },
  });
  const page = await context.newPage();

  try {
    await page.goto(APP_URL, { waitUntil: 'networkidle', timeout: 30000 });
    await page.waitForSelector('.brand', { timeout: 10000 });
    console.log('[Setup] Application responded cleanly.');
  } catch (err) {
    console.warn('[Setup] Note: Pre-flight load warning:', err.message);
  }

  // Save session storageState
  await context.storageState({ path: AUTH_STATE_PATH });
  await context.close();
  await browser.close();
  console.log(`[Setup] Session state saved to ${path.basename(AUTH_STATE_PATH)}.\n`);
}

async function runRecording() {
  if (!fs.existsSync(DURATIONS_PATH) || !fs.existsSync(NARRATION_PATH)) {
    console.error('Error: narration.json or durations.json missing! Run gen_audio.py first.');
    process.exit(1);
  }

  const narration = JSON.parse(fs.readFileSync(NARRATION_PATH, 'utf-8'));
  const durations = JSON.parse(fs.readFileSync(DURATIONS_PATH, 'utf-8'));

  // Ensure recording directory exists
  if (!fs.existsSync(RECORDINGS_DIR)) {
    fs.mkdirSync(RECORDINGS_DIR, { recursive: true });
  }

  // Step 1: Pre-flight auth setup
  await performPreflightSetup();

  console.log('[Record] Launching Chromium with recording enabled (1920x1080)...');
  const browser = await chromium.launch({
    headless: false,
    args: ['--start-maximized', '--no-sandbox', '--disable-setuid-sandbox'],
  });

  const context = await browser.newContext({
    viewport: { width: 1920, height: 1080 },
    storageState: AUTH_STATE_PATH,
    recordVideo: {
      dir: RECORDINGS_DIR,
      size: { width: 1920, height: 1080 },
    },
  });

  const page = await context.newPage();
  console.log(`[Record] Navigating to ${APP_URL}...`);
  await page.goto(APP_URL, { waitUntil: 'networkidle', timeout: 30000 });
  await page.waitForSelector('.brand', { timeout: 15000 });

  // Inject fake visual cursor
  await injectFakeCursor(page);
  await page.mouse.move(960, 540);
  await sleep(400);

  const timings = {};
  const recordingStartTime = Date.now();

  console.log('\n--- Starting Demo Step Execution ---');

  for (let i = 0; i < narration.length; i++) {
    const step = narration[i];
    const stepId = step.id;
    const clipDuration = durations[stepId] || 10.0;
    const requiredStepDurationMs = (clipDuration * 1000) + PAUSE_AFTER_STEP_MS;

    const stepStartOffsetSec = (Date.now() - recordingStartTime) / 1000;
    timings[stepId] = parseFloat(stepStartOffsetSec.toFixed(3));

    console.log(`\n▶ [Step ${i + 1}/${narration.length}] ${stepId} (offset: ${stepStartOffsetSec.toFixed(2)}s, budget: ${(requiredStepDurationMs / 1000).toFixed(2)}s)`);
    console.log(`  Narration: "${step.narration}"`);

    const stepActionStartTime = Date.now();

    // Execute step-specific realistic user UI actions
    try {
      switch (stepId) {
        case '01_problem_intro':
          // Smooth glance across Workbench sidebar, top command strip, and center canvas
          await smoothMove(page, 'aside.sidebar');
          await sleep(500);
          await smoothMove(page, '.command-group');
          await sleep(600);
          await smoothMove(page, '#graph');
          break;

        case '02_architecture_topology':
          // Hover over graph and click a service node (e.g. checkout_api, app_service, app_cache)
          await smoothMove(page, '#graph');
          await sleep(300);
          const serviceNode = page.locator('g.node').nth(1);
          if (await serviceNode.isVisible().catch(() => false)) {
            await smoothClick(page, serviceNode);
          } else {
            await smoothClick(page, 'g.node');
          }
          await sleep(400);
          // Smoothly glide over to Node Inspector in the right sidebar
          await smoothMove(page, '#ih');
          break;

        case '03_select_scenario':
          // Select 03_cache_kill_at_peak or database_5x_slowdown from scenario dropdown
          const scenarioSelect = page.locator('.pick select').nth(1);
          await smoothClick(page, scenarioSelect);
          const hasCacheKill = await scenarioSelect.locator('option[value="03_cache_kill_at_peak"]').count();
          if (hasCacheKill > 0) {
            await scenarioSelect.selectOption('03_cache_kill_at_peak');
          } else {
            await scenarioSelect.selectOption('database_5x_slowdown');
          }
          await sleep(400);
          // Highlight scenario events list
          await smoothMove(page, '#sh');
          await sleep(300);
          await smoothMove(page, 'ul.events li:first-child');
          break;

        case '04_run_simulation':
          // Click "Run scenario" button
          await smoothClick(page, 'button.run');
          // Wait for simulation spinner overlay to complete
          await page.waitForSelector('.sim-overlay', { state: 'attached', timeout: 5000 }).catch(() => {});
          await page.waitForSelector('.sim-overlay', { state: 'detached', timeout: 25000 }).catch(() => {});
          await sleep(400);
          break;

        case '05_incident_verdict':
          // Inspect Verdict card and KPIs
          await smoothMove(page, '#vh');
          await sleep(400);
          await smoothMove(page, '.verdict');
          await sleep(400);
          await smoothMove(page, '.kpis');
          break;

        case '06_failure_story':
          // Scroll and inspect Failure Story in the right rail
          await smoothMove(page, '#sth');
          await sleep(300);
          const storyItem = page.locator('ol.story li').first();
          if (await storyItem.isVisible().catch(() => false)) {
            await smoothClick(page, storyItem);
          }
          break;

        case '07_timeline_scrubber':
          // Play and scrub the timeline
          const playBtn = page.locator('button.play').first();
          if (await playBtn.isVisible().catch(() => false)) {
            await smoothClick(page, playBtn);
            await sleep(1500);
            await smoothClick(page, playBtn); // pause
          }
          // Drag scrubber across error peak
          const tlSvg = page.locator('#timeline').first();
          if (await tlSvg.isVisible().catch(() => false)) {
            const box = await tlSvg.boundingBox();
            if (box) {
              const startX = box.x + box.width * 0.35;
              const endX = box.x + box.width * 0.65;
              const centerY = box.y + box.height * 0.5;
              await page.mouse.move(startX, centerY, { steps: 25 });
              await page.mouse.down();
              await page.mouse.move(endX, centerY, { steps: 25 });
              await page.mouse.up();
            }
          }
          break;

        case '08_ai_adversary':
          // Inspect AI Adversary stress scenarios
          const advHead = page.locator('h2:has-text("AI adversary"), #ah');
          if (await advHead.isVisible().catch(() => false)) {
            await smoothMove(page, advHead);
          }
          await sleep(300);
          const advRows = page.locator('.adv-row');
          if (await advRows.count() > 1) {
            await smoothMove(page, advRows.nth(1));
            await sleep(400);
            await smoothMove(page, advRows.nth(2));
          }
          break;

        case '09_targeted_remediation':
          // Examine AI Remediation / Targeted Fixes
          await smoothMove(page, '#fh');
          await sleep(400);
          const fixCard = page.locator('.fix-card, #finds .find').first();
          if (await fixCard.isVisible().catch(() => false)) {
            await smoothMove(page, fixCard);
          }
          break;

        case '10_apply_and_compare':
          // Apply fix and view comparison
          const applyFixBtn = page.locator('button.apply:not(.applied), button:has-text("Simulate selected fixes"), button:has-text("Apply & re-simulate")').first();
          if (await applyFixBtn.isVisible().catch(() => false)) {
            await smoothClick(page, applyFixBtn);
            await page.waitForSelector('.sim-overlay', { state: 'detached', timeout: 20000 }).catch(() => {});
            await sleep(600);
          }
          // Switch to Compare mode
          const compareNav = page.locator('button.nav-item:has-text("Compare"), .seg button:has-text("Compare")').first();
          if (await compareNav.isVisible().catch(() => false)) {
            await smoothClick(page, compareNav);
            await sleep(500);
            await smoothMove(page, '.compare-panel, .compare-grid, #timeline');
          }
          break;

        case '11_ai_prompt_drawer':
          // Showcase the bottom prompt dock
          const dockInput = page.locator('textarea.dock-input').first();
          if (await dockInput.isVisible().catch(() => false)) {
            await smoothClick(page, dockInput);
            await dockInput.pressSequentially('Add 2 read replicas with read fallback', { delay: 60 });
            await sleep(400);
            await smoothMove(page, 'button.dock-send');
            await sleep(400);
          } else {
            // Fallback to top Prompt & Edit button
            const promptBtn = page.locator('button.action-btn:has-text("Prompt & Edit")');
            if (await promptBtn.isVisible().catch(() => false)) {
              await smoothClick(page, promptBtn);
              await page.waitForSelector('.drawer', { state: 'visible', timeout: 5000 });
              await sleep(600);
              await smoothClick(page, '.drawer-close');
            }
          }
          break;

        case '12_tech_stack_summary':
          // Return to Architecture in Workbench and display clean canvas
          const archNav = page.locator('button.nav-item:has-text("Architecture"), .seg button:has-text("Before")').first();
          if (await archNav.isVisible().catch(() => false)) {
            await smoothClick(page, archNav);
          }
          await sleep(400);
          await smoothMove(page, '#graph');
          await sleep(600);
          await smoothMove(page, '.brand');
          await sleep(800);
          break;

        default:
          await smoothMove(page, '#graph');
          break;
      }
    } catch (actionErr) {
      console.warn(`  [Warning] Action error in step ${stepId}:`, actionErr.message);
    }

    // Step pacing: Wait for the exact audio duration + pause
    const actionElapsedMs = Date.now() - stepActionStartTime;
    const remainingStepMs = requiredStepDurationMs - actionElapsedMs;

    if (remainingStepMs > 0) {
      await sleep(remainingStepMs);
    } else {
      console.warn(`  [Notice] Step ${stepId} UI actions took ${actionElapsedMs}ms (budget: ${requiredStepDurationMs}ms).`);
    }
  }

  // Save timings.json
  fs.writeFileSync(TIMINGS_PATH, JSON.stringify(timings, null, 2), 'utf-8');
  console.log(`\n[Record] Timings logged successfully to ${path.basename(TIMINGS_PATH)}.`);

  // Graceful shutdown to finalize video recording
  await sleep(1000);
  const video = page.video();
  await page.close();
  await context.close();
  await browser.close();

  if (video) {
    const videoPath = await video.path();
    console.log(`[Record] Video saved by Playwright to: ${videoPath}`);
  }

  console.log('[Record] Recording session finished successfully!\n');
}

runRecording().catch((err) => {
  console.error('[Record] Fatal error:', err);
  process.exit(1);
});
