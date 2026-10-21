'use strict';
const token = location.hash.slice(1) || sessionStorage.getItem('medley-token') || '';
sessionStorage.setItem('medley-token', token);
history.replaceState(null, '', '/');
const $ = id => document.getElementById(id);
let model, savedRules;
async function request(path, data) {
  const response = await fetch(path, {method: data === undefined ? 'GET' : 'POST', headers: {'X-Medley-Token': token, 'Content-Type': 'application/json'}, body: data === undefined ? undefined : JSON.stringify(data)});
  const result = await response.json();
  if (response.status === 404) throw new Error('This settings tab uses an older Medley server. Reopen Medley with herdr plugin action invoke medley.settings and use the new tab.');
  if (!response.ok) throw new Error(result.error || 'The request failed. Try again.');
  return result;
}
async function run(message, operation, pending = 'Saving…', recover = true) {
  const focusedId = document.activeElement.id;
  $('error').hidden = true;
  $('feedback').hidden = false;
  $('feedback').textContent = pending;
  document.querySelectorAll('button,input,select').forEach(element => element.disabled = true);
  try {const result = await operation(); $('feedback').textContent = result || message; $('feedback').hidden = !$('feedback').textContent;}
  catch (error) {if (recover) {try {await load();} catch (_) {}} $('error').textContent = error.message; $('error').hidden = false; $('feedback').textContent = 'Changes could not be completed. Try again.';}
  finally {document.querySelectorAll('button,input,select').forEach(element => element.disabled = false); if(focusedId && $(focusedId)) $(focusedId).focus();}
}
function option(value, text) {const item = document.createElement('option'); item.value = value; item.textContent = text; return item;}
function closePreview() {
  $('player').pause();
  if ($('player').src) URL.revokeObjectURL($('player').src);
  $('player').removeAttribute('src'); $('player').hidden = true;
  $('preview-row').hidden = true;
  document.querySelector('main').append($('preview-row'));
}
function previewSound(data, row) {
  closePreview();
  return run('', async () => {
      const response = await fetch('/preview', {method:'POST',headers:{'X-Medley-Token':token,'Content-Type':'application/json'},body:JSON.stringify(data)});
      if (!response.ok) throw new Error((await response.json()).error);
      if (response.headers.get('Content-Type').includes('application/json')) {return 'This sound is set to quiet.';}
      if ($('player').src) URL.revokeObjectURL($('player').src);
      $('player').pause();
      $('player').src=URL.createObjectURL(await response.blob());$('player').hidden=false;
      row.append($('preview-row')); $('preview-row').hidden=false;
      try {await $('player').play();}
      catch (_) {throw new Error('The browser could not start the preview. Press Play in the audio controls to try again.');}
  }, 'Loading preview…', false);
}
function drawRules() {
  const workspace = $('workspace').value;
  if ($('rules').contains($('preview-row'))) closePreview();
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
    preview.onclick = () => previewSound({workspace,status}, row);
    const reset = document.createElement('button'); reset.textContent = 'Reset'; reset.setAttribute('aria-label','Reset ' + label.textContent + ' to default');
    reset.onclick = () => run('Sound reset to default.',async () => {await request('/rule',{workspace,status,choice:'default'});await load();});
    row.append(label,select,preview,reset); $('rules').append(row);
  }
}
function drawLibrary() {
  document.querySelector('.workspace-sounds-layout').style.setProperty('--panel-rows',Math.max(6,model.audio.length) + 1);
  if ($('library-files').contains($('preview-row'))) closePreview();
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
        closePreview();
        savedRules=undefined; $('undo').hidden=true;
        await load(); $('choose-audio').focus();
      });
    };
    const rename = document.createElement('button'); rename.textContent = 'Rename';
    rename.id = 'rename-' + audio.id;
    rename.setAttribute('aria-label','Rename ' + name.textContent);
    rename.onclick = () => editSoundName(audio, row, rename.id);
    const actions = document.createElement('div'); actions.className = 'sound-actions';
    const preview = document.createElement('button'); preview.textContent = 'Preview';
    preview.id = 'preview-' + audio.id; preview.setAttribute('aria-label','Preview ' + name.textContent);
    preview.onclick = () => previewSound({id:audio.id}, row);
    actions.append(preview,rename,remove);
    row.append(name,actions); $('library-files').append(row);
  }
  if (!model.audio.length) $('library-files').textContent='No imported sounds.';
}
function editSoundName(audio, row, returnFocus) {
  if (row.contains($('preview-row'))) closePreview();
  const form = document.createElement('form'); form.className = 'rename-form';
  const label = document.createElement('label'); label.textContent = 'Sound name';
  const input = document.createElement('input'); input.type = 'text'; input.required = true; input.maxLength = 160;
  input.id = 'sound-name-' + audio.id; label.htmlFor = input.id;
  input.value = audio.name.replace(/\.mp3$/i,'');
  const error = document.createElement('p'); error.role = 'alert'; error.className = 'rename-error'; error.hidden = true;
  error.id = input.id + '-error'; input.setAttribute('aria-describedby',error.id);
  const save = document.createElement('button'); save.type = 'submit'; save.textContent = 'Save';
  const cancel = document.createElement('button'); cancel.type = 'button'; cancel.textContent = 'Cancel';
  const actions = document.createElement('div'); actions.className = 'sound-actions'; actions.append(save,cancel);
  const close = () => {drawLibrary(); $(returnFocus)?.focus();};
  cancel.onclick = close;
  form.onkeydown = event => {if (event.key === 'Escape' && !save.disabled) {event.preventDefault();close();}};
  form.onsubmit = async event => {
    event.preventDefault(); error.hidden = true;
    save.disabled = true; input.disabled = true; cancel.disabled = true; save.textContent = 'Saving…';
    try {
      await request('/rename',{id:audio.id,name:input.value});
      await load(); $('feedback').hidden = false; $('feedback').textContent = 'Sound renamed.';
      $(returnFocus)?.focus();
    } catch (failure) {error.textContent = failure.message; error.hidden = false;}
    finally {save.disabled = false; input.disabled = false; cancel.disabled = false; save.textContent = 'Save'; if (form.isConnected) input.focus();}
  };
  form.append(label,input,actions,error); row.replaceChildren(form); input.focus(); input.select();
}
async function load() {
  const selected = $('workspace').value;
  model = await request('/api');
  $('enabled').checked = model.settings.enabled;
  $('enabled-label').textContent = model.settings.enabled ? 'Enabled' : 'Disabled';
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
function importAudio(file) {
  if (!file) return;
  return run('Audio imported. Select it for any status below.',async () => {
  if (!file || !(/\.(mp3|wav)$/i).test(file.name) || file.size > 20*1024*1024) throw new Error('Choose an MP3 or WAV file up to 20 MB.');
  const content = await new Promise((resolve,reject) => {const reader = new FileReader();reader.onload = () => resolve(reader.result.split(',')[1]);reader.onerror = () => reject(new Error('The file could not be read.'));reader.readAsDataURL(file);});
  await request('/upload',{name:file.name,content});$('upload').value = '';await load();
  });
};
load().catch(error => { $('error').textContent=error.message; $('error').hidden=false; });

$('uninstall').onclick = () => {
  if (!confirm('Uninstall Medley? Herdr’s previous sound setting will be restored first. Your imported sounds and saved choices will be kept.')) return;
  return run('Medley uninstalled. You can close this tab.',async () => {
    await request('/uninstall',{});
    $('player').pause();
    $('enabled').checked=false; $('enabled-label').textContent='Disabled';
    document.querySelectorAll('section,.settings-controls').forEach(section=>section.hidden=true);
  });
};

$('upload').onchange = () => importAudio($('upload').files[0]);
$('choose-audio').onclick = async () => {
  if (!window.showOpenFilePicker) {$('upload').click();return;}
  try {
    const [handle] = await window.showOpenFilePicker({startIn:'downloads',types:[{description:'Audio files',accept:{'audio/mpeg':['.mp3'],'audio/wav':['.wav']}}],excludeAcceptAllOption:true,multiple:false});
    await importAudio(await handle.getFile());
  } catch (error) {
    if (error.name === 'AbortError') return;
    $('error').textContent = 'The file picker could not open. Try again. ' + error.message; $('error').hidden = false;
  }
};

$('relaunch').onclick = () => run('', async () => {
  closePreview();
  await request('/relaunch', {});
  await new Promise(resolve => setTimeout(resolve, 750));
  for (let attempt = 0; attempt < 30; attempt++) {
    try {
      const response = await fetch('/api', {headers:{'X-Medley-Token':token}, signal:AbortSignal.timeout(1500)});
      if (response.ok) {location.reload(); return;}
    } catch (_) {}
    await new Promise(resolve => setTimeout(resolve, 500));
  }
  throw new Error('Medley has not reopened yet. Refresh this tab to try again.');
}, 'Relaunching Medley…', false);
