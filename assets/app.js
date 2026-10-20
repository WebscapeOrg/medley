'use strict';
const token = location.hash.slice(1) || sessionStorage.getItem('medley-token') || '';
sessionStorage.setItem('medley-token', token);
history.replaceState(null, '', '/');
const $ = id => document.getElementById(id);
let model, savedRules;
async function request(path, data) {
  const response = await fetch(path, {method: data === undefined ? 'GET' : 'POST', headers: {'X-Medley-Token': token, 'Content-Type': 'application/json'}, body: data === undefined ? undefined : JSON.stringify(data)});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'The request failed. Try again.');
  return result;
}
async function run(message, operation) {
  const focusedId = document.activeElement.id;
  $('error').hidden = true;
  $('feedback').textContent = 'Saving…';
  document.querySelectorAll('button,input,select').forEach(element => element.disabled = true);
  try {const result = await operation(); $('feedback').textContent = result || message;}
  catch (error) {try {await load();} catch (_) {} $('error').textContent = error.message; $('error').hidden = false; $('feedback').textContent = 'Changes could not be completed. Try again.';}
  finally {document.querySelectorAll('button,input,select').forEach(element => element.disabled = false); if(focusedId && $(focusedId)) $(focusedId).focus();}
}
function option(value, text) {const item = document.createElement('option'); item.value = value; item.textContent = text; return item;}
function drawRules() {
  const workspace = $('workspace').value;
  $('rules').replaceChildren();
  if (!workspace) {$('rules').textContent = 'No workspaces found. Open a herdr workspace, then refresh.'; return;}
  for (const status of model.statuses) {
    const row = document.createElement('div'); row.className = 'rule';
    const label = document.createElement('label'); label.htmlFor = 'status-' + status;
    label.textContent = ({idle:'Idle',working:'Working',blocked:'Needs attention',done:'Done',unknown:'Unknown'})[status];
    const select = document.createElement('select'); select.id = label.htmlFor;
    select.append(option('default','Default'), option('none','None · quiet'), ...model.audio.slice().sort((a,b) => a.name.replace(/\.mp3$/i,'').localeCompare(b.name.replace(/\.mp3$/i,''),undefined,{sensitivity:'base',numeric:true})).map(audio => option(audio.id,audio.name.replace(/\.mp3$/i,''))));
    select.value = model.settings.rules[workspace]?.[status] || 'default';
    select.onchange = () => run('Sound saved.', async () => {await request('/rule',{workspace,status,choice:select.value}); await load();});
    const preview = document.createElement('button'); preview.textContent = 'Preview'; preview.setAttribute('aria-label','Preview ' + label.textContent + ' sound');
    preview.onclick = () => run('Playing preview. Use the audio controls to stop.', async () => {
      const response = await fetch('/preview', {method:'POST',headers:{'X-Medley-Token':token,'Content-Type':'application/json'},body:JSON.stringify({workspace,status})});
      if (!response.ok) throw new Error((await response.json()).error);
      if (response.headers.get('Content-Type').includes('application/json')) {$('player').pause();$('player').hidden=true;return 'This sound is set to quiet.';}
      if ($('player').src) URL.revokeObjectURL($('player').src);
      $('player').pause();
      $('player').src=URL.createObjectURL(await response.blob());$('player').hidden=false;
      try {await $('player').play();}
      catch (_) {throw new Error('The browser could not start the preview. Press Play in the audio controls to try again.');}
    });
    const reset = document.createElement('button'); reset.textContent = 'Reset'; reset.setAttribute('aria-label','Reset ' + label.textContent + ' to default');
    reset.onclick = () => run('Sound reset to default.',async () => {await request('/rule',{workspace,status,choice:'default'});await load();});
    row.append(label,select,preview,reset); $('rules').append(row);
  }
}
function drawLibrary() {
  $('library-files').replaceChildren();
  for (const audio of model.audio.slice().sort((a,b)=>a.name.localeCompare(b.name,undefined,{sensitivity:'base',numeric:true}))) {
    const row = document.createElement('div'); row.className = 'library-file';
    const name = document.createElement('span'); name.textContent = audio.name.replace(/\.mp3$/i,'');
    const remove = document.createElement('button'); remove.textContent = 'Remove';
    remove.setAttribute('aria-label','Remove ' + name.textContent);
    remove.onclick = () => {
      if (!confirm('Remove “' + name.textContent + '” from the library? Every workspace/status using it will return to Default. You can import the file again later.')) return;
      return run('Sound removed. Choices using it now use Default.',async () => {
        await request('/remove',{id:audio.id});
        $('player').pause(); $('player').hidden=true;
        savedRules=undefined; $('undo').hidden=true;
        await load(); $('manage-library').focus();
      });
    };
    row.append(name,remove); $('library-files').append(row);
  }
  if (!model.audio.length) $('library-files').textContent='No imported sounds.';
}
$('manage-library').onclick = () => {
  const expanded = $('manage-library').getAttribute('aria-expanded') !== 'true';
  $('manage-library').setAttribute('aria-expanded',String(expanded));
  $('library-files').hidden = !expanded;
};
async function load() {
  const selected = $('workspace').value;
  model = await request('/api');
  $('enabled').checked = model.settings.enabled;
  $('library').textContent = model.audio.length ? model.audio.length + ' audio files available.' : 'Your library is empty. Import an MP3 to get started.';
  $('workspace').replaceChildren(...model.workspaces.map(w => option(w.workspace_id,w.label || w.workspace_id)));
  if (model.workspaces.some(w => w.workspace_id === selected)) $('workspace').value = selected;
  drawRules();
  drawLibrary();
}
$('workspace').onchange = drawRules;
$('refresh').onclick = () => run('Workspaces refreshed.',load);
$('enabled').onchange = () => run('Playback setting saved.',async () => {await request('/enabled',{enabled:$('enabled').checked});await load();});
$('reset').onclick = () => run('All sounds reset to default. You can undo this reset.',async () => {savedRules = structuredClone(model.settings.rules);await request('/reset',{});await load();$('undo').hidden = false;});
$('undo').onclick = () => run('Previous sound choices restored.',async () => {await request('/restore',{rules:savedRules});await load();$('undo').hidden = true;});
$('upload').onchange = () => {
  if (!$('upload').files.length) return;
  return run('Audio imported. Select it for any status below.',async () => {
  const file = $('upload').files[0];
  if (!file || !file.name.toLowerCase().endsWith('.mp3') || file.size > 20*1024*1024) throw new Error('Choose an MP3 file up to 20 MB.');
  const content = await new Promise((resolve,reject) => {const reader = new FileReader();reader.onload = () => resolve(reader.result.split(',')[1]);reader.onerror = () => reject(new Error('The file could not be read.'));reader.readAsDataURL(file);});
  await request('/upload',{name:file.name,content});$('upload').value = '';await load();
  });
};
run('Settings ready. Changes save immediately.',load);
