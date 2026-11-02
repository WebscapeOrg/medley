const assert = require('node:assert/strict');

module.exports = async function checkStatusMute(page, base, token, custom) {
  const headers = {'X-Medley-Token': token};
  const data = {workspace: 'w1', status: 'done', muted: true};
  assert.equal((await page.request.post(base + '/status-mute', {data})).status(), 403);
  assert.equal((await page.request.post(base + '/status-mute', {headers, data: {...data, muted: 'false'}})).status(), 400);
  const settings = async () => (await (await page.request.get(base + '/api', {headers})).json()).settings;
  const original = (await settings()).rules;
  const done = page.locator('#mute-done');
  const all = page.locator('#workspace-mute');
  const saved = async () => page.getByText('Sound saved.', {exact: true}).waitFor();
  const muted = async () => page.getByText('Muted. Saved sound choices are kept.', {exact: true}).waitFor();
  const unmuted = async () => page.getByText('Unmuted. Saved sound choices restored.', {exact: true}).waitFor();

  await page.evaluate(() => {model.settings.rules.w1.done = 'missing'; drawRules();});
  assert.equal(await page.locator('.rule').count(), 5);
  assert.equal(await page.locator('#status-done option:checked').textContent(), 'Missing sound');
  await page.evaluate(() => {model.settings.mutes = {w1: ['done']}; drawRules();});
  assert.equal(await page.locator('#status-done').inputValue(), 'none');
  assert.equal(await done.textContent(), 'Unmute');
  await page.evaluate(() => load());

  let release;
  const gate = new Promise(resolve => {release = resolve;});
  await page.route('**/status-mute', async route => {await gate; await route.continue();});
  await done.focus();
  await page.keyboard.press('Enter');
  await page.getByText('Saving…', {exact: true}).waitFor();
  assert.equal(await done.isDisabled(), true);
  release();
  await muted();
  await page.unroute('**/status-mute');
  assert.equal(await done.textContent(), 'Unmute');
  assert.equal(await done.evaluate(el => el === document.activeElement), true);
  assert.equal(await page.locator('#status-done').inputValue(), 'none');
  assert.equal(await page.locator('#status-done').isDisabled(), true);
  assert.deepEqual((await settings()).rules, original);
  await page.getByRole('button', {name: 'Preview Done sound', exact: true}).click();
  await page.getByText('This sound is set to quiet.', {exact: true}).waitFor();
  assert.equal(await page.locator('#status-done').isDisabled(), true);
  assert.equal(await page.locator('#player').isVisible(), false);
  await page.reload();
  await done.waitFor();
  assert.equal(await done.textContent(), 'Unmute');
  await page.locator('#workspace').selectOption('w2');
  assert.equal(await done.textContent(), 'Mute');
  assert.equal(await page.locator('#status-done').inputValue(), 'default');
  await page.locator('#workspace').selectOption('w1');
  await done.focus();
  await page.keyboard.press('Space');
  await unmuted();
  assert.equal(await page.locator('#status-done').inputValue(), custom);

  await page.locator('#status-working').selectOption('none');
  await saved();
  const beforeAll = (await settings()).rules;
  await all.click();
  await muted();
  assert.equal(await all.textContent(), 'Unmute all');
  assert.deepEqual(await page.locator('.rule select').evaluateAll(els => els.map(el => [el.value, el.disabled])), Array(5).fill(['none', true]));
  assert.deepEqual((await settings()).rules, beforeAll);
  assert.equal(await all.isEnabled(), true);
  assert.deepEqual(await page.locator('.rule-actions button:first-child').evaluateAll(els => els.map(el => el.disabled)), Array(5).fill(true));
  await page.getByRole('button', {name: 'Preview Done sound', exact: true}).click();
  await page.getByText('This sound is set to quiet.', {exact: true}).waitFor();
  assert.equal(await done.isDisabled(), true);
  await all.click();
  await unmuted();
  assert.equal(await done.isEnabled(), true);
  assert.equal(await page.locator('#status-done').inputValue(), custom);
  assert.equal(await page.locator('#status-working').inputValue(), 'none');
  assert.equal(await page.locator('#status-idle').inputValue(), 'default');

  await done.click();
  await muted();
  await page.route('**/status-mute', route => route.fulfill({status: 503, contentType: 'application/json', body: JSON.stringify({error: 'Could not save. Try again.'})}));
  await done.click();
  await page.getByText('Could not save. Try again.', {exact: true}).waitFor();
  assert.equal(await done.textContent(), 'Unmute');
  assert.equal(await done.isEnabled(), true);
  assert.equal(await page.locator('#status-done').isDisabled(), true);
  await page.unroute('**/status-mute');
  await done.click();
  await unmuted();

  await all.click();
  await muted();
  await page.locator('#reset').click();
  await page.getByText('All sounds reset to default. You can undo this reset.', {exact: true}).waitFor();
  assert.equal(await all.textContent(), 'Unmute all');
  await page.locator('#undo').click();
  await page.getByText('Previous sound choices restored.', {exact: true}).waitFor();
  await all.click();
  await unmuted();
  assert.equal(await page.locator('#status-done').inputValue(), custom);
  await done.click();
  await muted();

  for (const width of [1600, 1280, 900, 834, 768, 640, 390, 320]) {
    await page.setViewportSize({width, height: 1000});
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'overflow at ' + width);
    const controlsFit = await page.locator('.rule').evaluateAll(rows => rows.every(row => {
      const parent = row.getBoundingClientRect();
      const controls = [...row.querySelectorAll('select,button')].map(el => el.getBoundingClientRect());
      return controls.every(rect => rect.left >= parent.left && rect.right <= parent.right + 1 && rect.height >= 44)
        && controls.every((a, i) => controls.slice(i + 1).every(b => a.right + 11 <= b.left || b.right + 11 <= a.left || a.bottom + 11 <= b.top || b.bottom + 11 <= a.top));
    }));
    assert.ok(controlsFit, 'crowded controls at ' + width);
  }
  await page.setViewportSize({width: 1600, height: 1000});
  await page.screenshot({path: 'docs/mute-desktop.png', fullPage: true});
  await page.evaluate(() => {document.documentElement.style.fontSize = '200%';});
  for (const width of [1500, 1600]) {
    await page.setViewportSize({width, height: 1000});
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    assert.ok(await page.locator('#status-done').evaluate(el => el.getBoundingClientRect().width >= 320), 'unreadable enlarged selector at ' + width);
  }
  await page.evaluate(() => {document.documentElement.style.fontSize = '';});
  await page.setViewportSize({width: 390, height: 1000});
  await page.screenshot({path: 'docs/mute-mobile.png', fullPage: true});
  await page.emulateMedia({colorScheme: 'dark', reducedMotion: 'reduce'});
  await page.screenshot({path: 'docs/mute-dark.png', fullPage: true});
  await page.setViewportSize({width: 1600, height: 1000});
  await page.screenshot({path: 'docs/mute-desktop-dark.png', fullPage: true});
  for (const colorScheme of ['light', 'dark']) {
    await page.emulateMedia({colorScheme});
    const metrics = await page.evaluate(() => ({
      fonts: [...new Set([...document.querySelectorAll('main *')].map(el => getComputedStyle(el).fontFamily))],
      headings: [...document.querySelectorAll('h1,h2')].map(el => ({text: el.textContent, size: getComputedStyle(el).fontSize})),
      buttons: [...document.querySelectorAll('.rule-actions button,#workspace-mute')].map(el => ({text: el.textContent, width: el.getBoundingClientRect().width, height: el.getBoundingClientRect().height})),
      mutedText: getComputedStyle(document.getElementById('status-done')).color,
      mutedBackground: getComputedStyle(document.getElementById('status-done')).backgroundColor,
    }));
    const luminance = rgb => rgb.match(/\d+/g).slice(0, 3).map(Number).map(value => value / 255).map(value => value <= .04045 ? value / 12.92 : ((value + .055) / 1.055) ** 2.4).reduce((sum, value, i) => sum + value * [.2126, .7152, .0722][i], 0);
    const values = [luminance(metrics.mutedText), luminance(metrics.mutedBackground)].sort((a, b) => a - b);
    const contrast = (values[1] + .05) / (values[0] + .05);
    assert.ok(contrast >= 4.5);
    console.log('Mute UI measurements:', JSON.stringify({colorScheme, ...metrics, mutedContrast: contrast.toFixed(2)}));
  }
  await page.emulateMedia({colorScheme: 'light'});
  await page.setViewportSize({width: 1280, height: 1000});
  await done.click();
  await unmuted();
  await page.locator('#status-working').selectOption('default');
  await saved();
  assert.deepEqual((await settings()).rules, original);
  await page.evaluate(() => {model.workspaces = []; document.getElementById('workspace').replaceChildren(); drawRules();});
  assert.equal(await all.isDisabled(), true);
  await page.locator('#refresh').click();
  await page.getByText('Workspaces refreshed.', {exact: true}).waitFor();
  assert.equal(await all.isEnabled(), true);
};
