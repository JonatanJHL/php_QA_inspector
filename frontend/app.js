// Local PHP QA Orchestrator Frontend Logic

const BACKEND_URL = ''; // Same origin requests

// State Variables
let currentFiles = [];
let selectedFile = null;
let currentConfig = null;

// DOM Elements
const phpDirInput = document.getElementById('php-dir-input');
const saveConfigBtn = document.getElementById('save-config-btn');
const ollamaUrlInput = document.getElementById('ollama-url-input');
const modelSelect = document.getElementById('model-select');
const providerSelect = document.getElementById('provider-select');

// Presets de modelo NVIDIA ya probados como confiables (a diferencia de
// Ollama, cuyos modelos se descubren dinámicamente vía /api/ollama/models,
// NVIDIA no tiene un endpoint de "modelos instalados" que consultar — es un
// servicio en la nube con catálogo fijo, así que aquí sí tiene sentido una
// lista curada en vez de intentar descubrirla).
const NVIDIA_MODEL_PRESETS = [
  { value: 'nvidia/llama-3.3-nemotron-super-49b-v1.5', label: 'Nemotron 49B (recomendado — probado y confiable)' },
  { value: 'meta/llama-3.1-8b-instruct', label: 'Llama 3.1 8B (más rápido, menos confiable)' },
  { value: 'meta/llama-3.3-70b-instruct', label: 'Llama 3.3 70B (lento en el tier gratuito)' },
];
const refreshModelsBtn = document.getElementById('refresh-models-btn');
const configStatus = document.getElementById('config-status');
const shareUrlContainer = document.getElementById('share-url-container');


const fileSearch = document.getElementById('file-search');
const fileList = document.getElementById('file-list');
const fileCount = document.getElementById('file-count');

const activeFileTitle = document.getElementById('active-file-title');
const activeFilePath = document.getElementById('active-file-path');
const btnRunAll = document.getElementById('btn-run-all');

// Global risk chip (header) — unifies danger_score (Impact tab) and
// gate_status (Desktop Test tab) into one visible verdict.
const globalRiskChip = document.getElementById('global-risk-chip');
const globalRiskIcon = document.getElementById('global-risk-icon');
const globalRiskText = document.getElementById('global-risk-text');

const tabBtns = document.querySelectorAll('.tab-btn');
const tabPanels = document.querySelectorAll('.tab-panel');

const codeDisplay = document.getElementById('code-display');
const copyCodeBtn = document.getElementById('copy-code-btn');

// Syntax Tab
const btnRunSyntax = document.getElementById('btn-run-syntax');
const syntaxStatusCard = document.getElementById('syntax-status-card');
const syntaxResultTitle = document.getElementById('syntax-result-title');
const syntaxResultDesc = document.getElementById('syntax-result-desc');
const syntaxConsole = document.getElementById('syntax-console');
const syntaxBadge = document.getElementById('syntax-badge');

// Desktop Test Tab
const btnRunDesktop = document.getElementById('btn-run-desktop');
const agentActiveModel = document.getElementById('agent-active-model');
const desktopLoading = document.getElementById('desktop-loading');
const desktopResult = document.getElementById('desktop-result');
const desktopBadge = document.getElementById('desktop-badge');
const desktopTimer = document.getElementById('desktop-timer');
const gateBanner = document.getElementById('gate-banner');
const flowDiagramEmpty = document.getElementById('flow-diagram-empty');
const flowDiagramContainer = document.getElementById('flow-diagram-container');

// Impact Tab DOM Elements
const btnRunImpact = document.getElementById('btn-run-impact');
const btnExportSarif = document.getElementById('btn-export-sarif');
const impactBadge = document.getElementById('impact-badge');
const riskIndexValue = document.getElementById('risk-index-value');
const riskProgress = document.getElementById('risk-progress');
const riskRating = document.getElementById('risk-rating');
const impactLevelValue = document.getElementById('impact-level-value');
const impactCount = document.getElementById('impact-count');
const dangerScoreValue = document.getElementById('danger-score-value');
const dangerCount = document.getElementById('danger-count');
const dangerFlagsList = document.getElementById('danger-flags-list');
const includesList = document.getElementById('includes-list');
const includedByDirectList = document.getElementById('included-by-direct-list');
const impactSetList = document.getElementById('impact-set-list');


// Initialize App
document.addEventListener('DOMContentLoaded', async () => {
  setupTabs();
  await loadConfig();
  await loadModels();
  await loadFiles();

  // Mermaid theme tuned to match the app's dark glassmorphism palette.
  // Uses fixed hex values (not CSS vars) because Mermaid reads this config
  // once at init time and doesn't re-resolve CSS custom properties per render.
  if (typeof mermaid !== 'undefined') {
    mermaid.initialize({
      startOnLoad: false,
      theme: 'dark',
      themeVariables: {
        background: '#0f0f1a',
        primaryColor: '#1e1b3a',
        primaryTextColor: '#e5e5f0',
        primaryBorderColor: '#8b5cf6',
        lineColor: '#6b7280',
        secondaryColor: '#10b981',
        tertiaryColor: '#1a1a2e'
      },
      securityLevel: 'strict'
    });
  }
  
  // Event Listeners
  saveConfigBtn.addEventListener('click', saveConfig);
  refreshModelsBtn.addEventListener('click', loadModels);
  fileSearch.addEventListener('input', filterFiles);
  copyCodeBtn.addEventListener('click', copyCodeToClipboard);
  
  btnRunSyntax.addEventListener('click', () => runSyntaxCheck(true));
  btnRunDesktop.addEventListener('click', runDesktopTest);
  btnRunImpact.addEventListener('click', runImpactAnalysis);
  btnExportSarif.addEventListener('click', exportSarif);
  btnRunAll.addEventListener('click', runFullOrchestratedQA);
});

// Setup Tabs Logic
function setupTabs() {
  tabBtns.forEach(btn => {
    btn.addEventListener('click', () => {
      const targetPanel = btn.getAttribute('data-tab');
      
      tabBtns.forEach(b => b.classList.remove('active'));
      tabPanels.forEach(p => p.classList.remove('active'));
      
      btn.classList.add('active');
      document.getElementById(targetPanel).classList.add('active');
    });
  });
}

// Load System Config
async function loadConfig() {
  try {
    const response = await fetch(`${BACKEND_URL}/api/config`);
    if (!response.ok) throw new Error('Error al obtener la configuración.');
    
    currentConfig = await response.json();
    phpDirInput.value = currentConfig.php_dir;
    ollamaUrlInput.value = currentConfig.ollama_url;
    
    if (!currentConfig.exists) {
      showConfigStatus('Directorio configurado no accesible localmente.', 'error');
      shareUrlContainer.classList.add('hidden');
    } else {
      showConfigStatus('Configuración cargada correctamente.', 'success');
      shareUrlContainer.classList.remove('hidden');
      shareUrlContainer.innerHTML = `<i class="fa-solid fa-share-nodes"></i> Compartir en red: <strong>http://${currentConfig.local_ip}:8000</strong>`;
    }
  } catch (error) {
    console.error(error);
    showConfigStatus('Error al conectar con el backend local.', 'error');
    shareUrlContainer.classList.add('hidden');
  }
}

// Save Config Changes
async function saveConfig() {
  const phpDir = phpDirInput.value.trim();
  const ollamaUrl = ollamaUrlInput.value.trim();
  
  if (!phpDir) {
    showConfigStatus('El directorio PHP no puede estar vacío.', 'error');
    return;
  }
  
  showConfigStatus('Guardando configuración...', '');
  shareUrlContainer.classList.add('hidden');
  
  try {
    const response = await fetch(`${BACKEND_URL}/api/config`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ php_dir: phpDir, ollama_url: ollamaUrl })
    });
    
    const result = await response.json();
    if (!response.ok) {
      throw new Error(result.detail || 'Error al guardar la configuración.');
    }
    
    currentConfig = result.config;
    showConfigStatus('Configuración guardada y actualizada.', 'success');
    shareUrlContainer.classList.remove('hidden');
    shareUrlContainer.innerHTML = `<i class="fa-solid fa-share-nodes"></i> Compartir en red: <strong>http://${currentConfig.local_ip}:8000</strong>`;
    
    // Reload everything
    await loadModels();
    await loadFiles();
  } catch (error) {
    showConfigStatus(error.message, 'error');
    shareUrlContainer.classList.add('hidden');
  }
}

function showConfigStatus(msg, type) {
  configStatus.textContent = msg;
  configStatus.className = 'status-msg';
  if (type) configStatus.classList.add(type);
}

// Load Models list from local Ollama
async function loadModels() {
  try {
    modelSelect.innerHTML = '<option value="">Cargando modelos...</option>';
    const response = await fetch(`${BACKEND_URL}/api/ollama/models`);
    if (!response.ok) throw new Error('Error de servidor.');
    
    const data = await response.json();
    modelSelect.innerHTML = '';
    
    if (data.models && data.models.length > 0) {
      data.models.forEach(model => {
        const option = document.createElement('option');
        option.value = model;
        option.textContent = model;
        // Prioriza el modelo configurado por default en el backend, o el
        // primer modelo con capability de coder si no coincide ninguno —
        // ya no asume que 'hermes3' existe (dependía de un modelo que puede
        // no estar instalado en este equipo).
        if (currentConfig && model === currentConfig.default_model) {
          option.selected = true;
        }
        modelSelect.appendChild(option);
      });
      if (!modelSelect.value && data.models.length > 0) {
        modelSelect.value = data.models[0];
      }
      showConfigStatus('Modelos de Ollama sincronizados.', 'success');
    } else {
      modelSelect.innerHTML = '<option value="">Ningún modelo disponible</option>';
      const warning = data.warning || 'No se encontraron modelos. ¿Ollama está encendido?';
      showConfigStatus(warning, 'error');
    }
    updateAgentModelIndicator();
  } catch (error) {
    modelSelect.innerHTML = '<option value="">No se pudo conectar a Ollama</option>';
    showConfigStatus('No se pudo conectar a Ollama.', 'error');
    updateAgentModelIndicator();
  }
}

// Handle Model Change Selection
modelSelect.addEventListener('change', () => {
  updateAgentModelIndicator();
});

// Cambiar de provider repuebla el selector de modelo: Ollama vuelve a
// consultar /api/ollama/models (lista real de lo instalado), NVIDIA pinta
// los presets fijos de arriba (no hay lista "instalada" que consultar en
// un servicio en la nube).
providerSelect.addEventListener('change', () => {
  if (providerSelect.value === 'nvidia') {
    modelSelect.innerHTML = NVIDIA_MODEL_PRESETS
      .map(p => `<option value="${p.value}">${p.label}</option>`).join('');
    showConfigStatus('Usando NVIDIA NIM — requiere NVIDIA_API_KEY configurada en el backend.', 'success');
    updateAgentModelIndicator();
  } else {
    loadModels();
  }
});

function updateAgentModelIndicator() {
  agentActiveModel.textContent = modelSelect.value || '-';
}

// Load Files from Configured Directory
async function loadFiles() {
  try {
    fileList.innerHTML = '<li class="loading-item"><i class="fa-solid fa-spinner fa-spin"></i> Escaneando servidor...</li>';
    const response = await fetch(`${BACKEND_URL}/api/files`);
    if (!response.ok) {
      const data = await response.json();
      throw new Error(data.detail || 'Error al escanear archivos.');
    }
    
    const data = await response.json();
    currentFiles = data.files || [];
    fileCount.textContent = currentFiles.length;
    
    renderFileList(currentFiles);
  } catch (error) {
    fileCount.textContent = '0';
    fileList.innerHTML = `<li class="loading-item text-danger"><i class="fa-solid fa-triangle-exclamation"></i> ${error.message}</li>`;
  }
}

// Render Files to Sidebar List
function renderFileList(files) {
  fileList.innerHTML = '';
  if (files.length === 0) {
    fileList.innerHTML = '<li class="loading-item">No se encontraron archivos compatibles (.php, .js, .tpl, .html, .css, .sql, .json).</li>';
    return;
  }
  
  files.forEach(file => {
    const li = document.createElement('li');
    li.dataset.path = file.full_path;
    
    const ext = file.name.split('.').pop().toLowerCase();
    let iconClass = 'fa-regular fa-file-code';
    let iconColor = 'var(--accent-purple)';
    
    if (ext === 'php') {
      iconClass = 'fa-brands fa-php';
      iconColor = '#8892bf';
    } else if (ext === 'js') {
      iconClass = 'fa-brands fa-js';
      iconColor = '#f7df1e';
    } else if (ext === 'tpl' || ext === 'html') {
      iconClass = 'fa-solid fa-code';
      iconColor = '#e34f26';
    } else if (ext === 'css') {
      iconClass = 'fa-brands fa-css3-alt';
      iconColor = '#1572b6';
    } else if (ext === 'sql') {
      iconClass = 'fa-solid fa-database';
      iconColor = '#00758f';
    } else if (ext === 'json') {
      iconClass = 'fa-solid fa-code-compare';
      iconColor = '#10b981';
    }
    
    const formattedSize = (file.size / 1024).toFixed(1) + ' KB';
    
    li.innerHTML = `
      <div class="file-name" title="${file.name}"><i class="${iconClass}" style="margin-right: 6px; color: ${iconColor}"></i>${file.name}</div>
      <div class="file-path-sub">${file.rel_path} (${formattedSize})</div>
    `;
    
    li.addEventListener('click', () => selectFile(file, li));
    fileList.appendChild(li);
  });
}

// Filter Files with search bar
function filterFiles() {
  const query = fileSearch.value.toLowerCase().trim();
  if (!query) {
    renderFileList(currentFiles);
    return;
  }
  
  const filtered = currentFiles.filter(file => 
    file.name.toLowerCase().includes(query) || 
    file.rel_path.toLowerCase().includes(query)
  );
  renderFileList(filtered);
}

// Select File to inspect & audit
async function selectFile(file, element) {
  // Clear active styling
  document.querySelectorAll('#file-list li').forEach(li => li.classList.remove('active'));
  element.classList.add('active');
  
  selectedFile = file;
  activeFileTitle.textContent = file.name;
  activeFilePath.textContent = file.full_path;
  btnRunAll.removeAttribute('disabled');
  
  // Reset outputs & badges
  resetOutputs();
  
  // Load Code Content
  codeDisplay.textContent = 'Cargando archivo...';
  try {
    const response = await fetch(`${BACKEND_URL}/api/file/content`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filepath: file.full_path })
    });
    
    if (!response.ok) {
      const err = await response.json();
      throw new Error(err.detail || 'No se pudo leer el archivo.');
    }
    
    const data = await response.json();
    codeDisplay.textContent = data.content;
    
    // Switch to source code tab by default
    switchTab('tab-code');
  } catch (error) {
    codeDisplay.textContent = `Error: ${error.message}`;
  }
}

function switchTab(tabId) {
  const btn = document.querySelector(`.tab-btn[data-tab="${tabId}"]`);
  if (btn) btn.click();
}

// Global risk state: tracks the worst known verdict across both analyses
// (Impacto y Riesgo, and Prueba de Escritorio) so the header chip always
// reflects the most severe signal seen so far for the selected file, no
// matter which tab produced it or which one the user is currently viewing.
// Ranking: bloqueado > requiere_confirmacion > aprobado > pendiente.
const RISK_RANK = { bloqueado: 3, requiere_confirmacion: 2, aprobado: 1, pendiente: 0 };
let _globalRiskState = { status: 'pendiente', source: null };

function resetGlobalRiskChip() {
  _globalRiskState = { status: 'pendiente', source: null };
  globalRiskChip.classList.add('hidden');
  globalRiskChip.className = 'global-risk-chip hidden risk-pendiente';
  globalRiskIcon.innerHTML = '<i class="fa-solid fa-shield-halved"></i>';
  globalRiskText.textContent = 'Sin analizar';
}

// Called from both runImpactAnalysis (danger_score derived) and
// runDesktopTest/renderGateBanner (gate_status from the LLM's matrix).
// Only upgrades severity — a later 'aprobado' from one tab never downgrades
// a 'bloqueado' already confirmed by the other, since that would hide a
// real finding just because a different check came back clean.
function updateGlobalRiskChip(status, source) {
  if (RISK_RANK[status] === undefined) return;
  if (RISK_RANK[status] < RISK_RANK[_globalRiskState.status]) return;

  _globalRiskState = { status, source };
  globalRiskChip.classList.remove('hidden');
  globalRiskChip.className = `global-risk-chip risk-${status}`;

  const labels = {
    bloqueado: { icon: '<i class="fa-solid fa-ban"></i>', text: 'Riesgo crítico — revisar antes de continuar' },
    requiere_confirmacion: { icon: '<i class="fa-solid fa-triangle-exclamation"></i>', text: 'Requiere confirmación' },
    aprobado: { icon: '<i class="fa-solid fa-circle-check"></i>', text: 'Sin hallazgos críticos' },
    pendiente: { icon: '<i class="fa-solid fa-shield-halved"></i>', text: 'Sin analizar' },
  };
  const label = labels[status] || labels.pendiente;
  globalRiskIcon.innerHTML = label.icon;
  globalRiskText.textContent = label.text;
}

// Clicking the chip jumps to whichever tab most recently raised the current
// severity level, so the user lands directly on the relevant detail instead
// of having to guess which tab to check.
globalRiskChip.addEventListener('click', () => {
  if (_globalRiskState.source === 'impact') {
    switchTab('tab-impact');
  } else if (_globalRiskState.source === 'desktop') {
    switchTab('tab-desktop');
  }
});

// Maps a numeric risk_index (0-100, from the Impact tab) to the same
// gate_status vocabulary the backend gate uses, so both analyses can be
// combined through a single ranking without special-casing each source.
function riskIndexToGateStatus(riskIndex) {
  if (riskIndex >= 65) return 'bloqueado';
  if (riskIndex >= 30) return 'requiere_confirmacion';
  return 'aprobado';
}

function resetOutputs() {
  resetGlobalRiskChip();

  // Reset syntax tab
  syntaxStatusCard.className = 'syntax-card status-idle';
  syntaxResultTitle.textContent = 'Pendiente de ejecución';
  syntaxResultDesc.textContent = 'Presiona el botón para validar la sintaxis con el compilador PHP.';
  syntaxConsole.textContent = '> Listo.';
  syntaxBadge.className = 'tab-badge dot';
  
  // Reset desktop test tab
  desktopResult.innerHTML = `
    <div class="empty-state">
      <i class="fa-solid fa-robot"></i>
      <h3>Simulación de Agente</h3>
      <p>Inicia el análisis para obtener una traza detallada de variables, flujo de datos y análisis de vulnerabilidades lógicas en base al modelo local seleccionado.</p>
    </div>
  `;
  desktopBadge.className = 'tab-badge dot';
  desktopLoading.classList.add('hidden');
  gateBanner.classList.add('hidden');
  gateBanner.innerHTML = '';
  flowDiagramEmpty.classList.remove('hidden');
  flowDiagramContainer.classList.add('hidden');
  flowDiagramContainer.innerHTML = '';

  // Reset impact tab
  impactBadge.className = 'tab-badge dot';
  riskIndexValue.textContent = '0%';
  riskProgress.style.width = '0%';
  riskRating.innerHTML = 'Calificación: N/A';
  impactLevelValue.textContent = '-';
  impactCount.textContent = '0 archivos afectados directamente';
  dangerScoreValue.textContent = '0%';
  dangerCount.textContent = '0 banderas de peligro';
  dangerFlagsList.innerHTML = '<li class="empty-list">Ningún peligro lógico detectado.</li>';
  includesList.innerHTML = '<li class="empty-list">No incluye a otros archivos.</li>';
  includedByDirectList.innerHTML = '<li class="empty-list">Ningún archivo lo incluye directamente.</li>';
  impactSetList.innerHTML = '<li class="empty-list">Aislado. Ningún archivo se verá afectado directamente.</li>';
}

// Copy Code View To Clipboard
function copyCodeToClipboard() {
  if (!selectedFile) return;
  navigator.clipboard.writeText(codeDisplay.textContent)
    .then(() => {
      const originalText = copyCodeBtn.innerHTML;
      copyCodeBtn.innerHTML = '<i class="fa-solid fa-check"></i> Copiado!';
      setTimeout(() => {
        copyCodeBtn.innerHTML = originalText;
      }, 2000);
    })
    .catch(err => {
      alert('Error al copiar el código: ' + err);
    });
}

// Exec Syntax Check (PHP -l)
async function runSyntaxCheck(shouldAlertSuccess = false) {
  if (!selectedFile) return false;
  
  syntaxBadge.className = 'tab-badge dot running';
  syntaxConsole.textContent = '> php -l ' + selectedFile.name + '\nEjecutando linter de PHP...';
  
  try {
    const response = await fetch(`${BACKEND_URL}/api/qa/syntax`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filepath: selectedFile.full_path })
    });
    
    if (!response.ok) throw new Error('Error al llamar al linter.');
    
    const result = await response.json();
    
    syntaxConsole.textContent = `> php -l "${selectedFile.full_path}"\n\n${result.output}`;
    
    if (result.success) {
      syntaxStatusCard.className = 'syntax-card status-success';
      syntaxResultTitle.textContent = 'Sintaxis Correcta';
      syntaxResultDesc.textContent = 'El compilador de PHP no detectó ningún error de sintaxis básica.';
      syntaxBadge.className = 'tab-badge dot success';
      return true;
    } else {
      syntaxStatusCard.className = 'syntax-card status-error';
      syntaxResultTitle.textContent = 'Error de Sintaxis Detectado';
      syntaxResultDesc.textContent = `Se detectó un fallo de parseo. Revisa el código cerca de la línea ${result.error_line || 'desconocida'}.`;
      syntaxBadge.className = 'tab-badge dot error';
      return false;
    }
  } catch (error) {
    syntaxConsole.textContent += `\n\nError de ejecución: ${error.message}`;
    syntaxStatusCard.className = 'syntax-card status-error';
    syntaxResultTitle.textContent = 'Fallo del Linter';
    syntaxResultDesc.textContent = 'Hubo un error al ejecutar el comando de verificación PHP.';
    syntaxBadge.className = 'tab-badge dot error';
    return false;
  }
}

// Exec Desktop Test Simulation with Ollama (Streaming)
let _desktopTimerInterval = null;
let _desktopStartTime = null;
let _desktopHeartbeatLabel = '';

function startDesktopTimer() {
  _desktopStartTime = Date.now();
  _desktopHeartbeatLabel = '';
  desktopTimer.classList.remove('hidden');
  updateDesktopTimerDisplay();
  _desktopTimerInterval = setInterval(updateDesktopTimerDisplay, 1000);
}

function updateDesktopTimerDisplay() {
  const elapsed = Math.floor((Date.now() - _desktopStartTime) / 1000);
  const mins = Math.floor(elapsed / 60);
  const secs = elapsed % 60;
  const timeStr = mins > 0 ? `${mins}m ${secs}s` : `${secs}s`;
  desktopTimer.textContent = _desktopHeartbeatLabel ? `${_desktopHeartbeatLabel} · ${timeStr}` : timeStr;
}

function stopDesktopTimer() {
  if (_desktopTimerInterval) {
    clearInterval(_desktopTimerInterval);
    _desktopTimerInterval = null;
  }
}

async function runDesktopTest() {
  if (!selectedFile) return;
  
  const model = modelSelect.value;
  desktopBadge.className = 'tab-badge dot running';
  desktopLoading.classList.remove('hidden');
  desktopResult.classList.add('hidden');
  desktopResult.innerHTML = '';
  gateBanner.classList.add('hidden');
  gateBanner.innerHTML = '';
  startDesktopTimer();

  // Watchdog de inactividad: la Fetch API no tiene NINGÚN timeout nativo por
  // datos que dejan de llegar (confirmado: es comportamiento estándar, no un
  // bug de un navegador en particular) — si la conexión se corta en silencio
  // en el camino (el navegador, el SO, o la red intermedia la dan por
  // muerta sin avisar), `reader.read()` puede quedarse esperando para
  // siempre sin lanzar ninguna excepción, dejando al usuario viendo
  // "Analizando..." indefinidamente sin saber si algo se rompió. Este
  // temporizador se reinicia cada vez que llega un chunk nuevo (heartbeat
  // incluido, que llegan cada ~4s mientras el modelo piensa) y aborta la
  // conexión si pasan más de INACTIVITY_TIMEOUT_MS sin ninguno.
  const INACTIVITY_TIMEOUT_MS = 90000; // 90s: más que el intervalo de heartbeat (~4s) con margen amplio
  const abortController = new AbortController();
  let inactivityTimer = null;
  function resetInactivityWatchdog() {
    if (inactivityTimer) clearTimeout(inactivityTimer);
    inactivityTimer = setTimeout(() => {
      abortController.abort();
    }, INACTIVITY_TIMEOUT_MS);
  }
  
  try {
    resetInactivityWatchdog();
    const response = await fetch(`${BACKEND_URL}/api/qa/desktop-test`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filepath: selectedFile.full_path, model: model, provider: providerSelect.value }),
      signal: abortController.signal
    });
    
    if (!response.ok) {
      const err = await response.json();
      throw new Error(err.detail || 'Ocurrió un error al iniciar el agente de Ollama.');
    }
    
    // Hide loading overlay as soon as we start receiving the stream
    desktopLoading.classList.add('hidden');
    desktopResult.classList.remove('hidden');
    
    const reader = response.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let accumulatedText = '';   // real content only — never includes heartbeat/gate lines
    let streamBuffer = '';      // raw incoming text, line-buffered before filtering
    let analysisComplete = false;
    let gateData = null;        // parsed __GATE__ JSON, if the backend sent one

    // Heartbeat lines arrive prefixed with "__HB__:" (see call_ollama_with_heartbeat
    // and analyze_block in the backend). Rather than appending each one as a
    // permanent new line in the rendered markdown (which previously filled the
    // whole screen with dozens of "Ollama sigue procesando..." lines that never
    // went away), we intercept them here and use them to update the single
    // timer/status indicator in place, then drop them from accumulatedText.
    function processBufferedLines(buffer, isFinal) {
      const lines = buffer.split('\n');
      // Keep the last (possibly incomplete) line in the buffer unless this is
      // the final flush, so we don't split a heartbeat line across chunks.
      const complete = isFinal ? lines : lines.slice(0, -1);
      const remainder = isFinal ? '' : lines[lines.length - 1];

      for (const line of complete) {
        if (line.startsWith('__HB__:')) {
          _desktopHeartbeatLabel = line.slice('__HB__:'.length).replace(/\s*\(\d+s transcurridos\)\s*$/, '').trim();
          updateDesktopTimerDisplay();
        } else if (line.startsWith('__GATE__:')) {
          try {
            gateData = JSON.parse(line.slice('__GATE__:'.length));
          } catch (e) {
            console.error('No se pudo parsear el veredicto del gate:', e);
          }
        } else {
          accumulatedText += line + '\n';
        }
      }
      return remainder;
    }
    
    while (true) {
      const { value, done } = await reader.read();
      resetInactivityWatchdog(); // llegó algo (o el stream cerró) — la conexión sigue viva
      if (done) {
        streamBuffer = processBufferedLines(streamBuffer, true);
        break;
      }
      
      const chunk = decoder.decode(value, { stream: true });
      streamBuffer += chunk;
      streamBuffer = processBufferedLines(streamBuffer, false);

      // The backend appends this exact marker once the full analysis (all
      // blocks + consolidation, or the single-call path) has truly finished.
      // Heartbeat lines during long CPU-bound waits mean the stream can go
      // quiet for minutes without this marker, so we can't infer completion
      // just from "no more chunks arriving yet".
      if (accumulatedText.includes('✅ **Análisis completo.**')) {
        analysisComplete = true;
      }
      
      // Render markdown in real-time, using only the filtered content —
      // heartbeat lines never reach this point, so they can't accumulate
      // as permanent lines in the result.
      if (window.marked) {
        desktopResult.innerHTML = marked.parse(accumulatedText);
      } else {
        desktopResult.innerHTML = `<pre style="white-space: pre-wrap; font-family: inherit;">${accumulatedText}</pre>`;
      }
    }

    if (accumulatedText.includes('✅ **Análisis completo.**')) {
      analysisComplete = true;
    }

    clearTimeout(inactivityTimer); // stream terminó normalmente — ya no hay que vigilar inactividad
    stopDesktopTimer();
    desktopBadge.className = analysisComplete ? 'tab-badge dot success' : 'tab-badge dot error';

    if (gateData) {
      renderGateBanner(gateData);
    }

    // Extract and render the Mermaid flowchart block, if the model produced one
    renderFlowDiagram(accumulatedText);
  } catch (error) {
    clearTimeout(inactivityTimer);
    stopDesktopTimer();
    desktopLoading.classList.add('hidden');
    desktopResult.classList.remove('hidden');
    // AbortError con nuestro propio abortController: fue el watchdog el que
    // cortó la conexión por inactividad, no un error real del servidor —
    // mensaje distinto para que el usuario entienda qué pasó y qué hacer.
    const isWatchdogAbort = error.name === 'AbortError';
    const title = isWatchdogAbort ? 'Se perdió la conexión con el servidor' : 'Error en la Prueba de Escritorio';
    const message = isWatchdogAbort
      ? `No se recibió respuesta del servidor por más de ${Math.round(INACTIVITY_TIMEOUT_MS / 1000)} segundos — la conexión pudo haberse cortado en el camino (navegador, red, o el servidor pudo haberse detenido). El análisis puede haber seguido corriendo del lado del servidor; revisa su consola o inténtalo de nuevo.`
      : error.message;
    desktopResult.innerHTML = `
      <div class="empty-state text-danger">
        <i class="fa-solid fa-triangle-exclamation"></i>
        <h3>${title}</h3>
        <p>${message}</p>
      </div>
    `;
    desktopBadge.className = 'tab-badge dot error';
  }
}

// Render the structured risk-gate verdict (parsed from the Matriz de Casos
// de Prueba on the backend) as a banner above the analysis. This is
// currently informational only — no action button is gated by it yet — but
// gives an unambiguous, non-skippable visual signal instead of relying on
// someone reading the whole table and judging severity themselves.
function renderGateBanner(gate) {
  gateBanner.classList.remove('hidden');
  gateBanner.className = `gate-banner gate-${gate.gate_status}`;

  updateGlobalRiskChip(gate.gate_status, 'desktop');

  let icon, title;
  if (gate.gate_status === 'bloqueado') {
    icon = '<i class="fa-solid fa-ban"></i>';
    title = 'BLOQUEADO: Riesgo crítico detectado';
  } else if (gate.gate_status === 'requiere_confirmacion') {
    icon = '<i class="fa-solid fa-triangle-exclamation"></i>';
    title = 'Requiere revisión antes de continuar';
  } else {
    icon = '<i class="fa-solid fa-circle-check"></i>';
    title = 'Sin fallas críticas detectadas en la matriz de casos';
  }

  let html = `<div class="gate-banner-title">${icon} ${title}</div>`;

  if (gate.fallas_criticas && gate.fallas_criticas.length > 0) {
    html += `<div><strong>Fallas críticas (${gate.fallas_criticas.length}):</strong><ul>`;
    gate.fallas_criticas.forEach(f => {
      html += `<li><strong>#${f.numero}</strong> [${f.tipo}] ${f.entrada} — <em>${f.resultado_esperado}</em> (${f.linea_o_funcion})</li>`;
    });
    html += `</ul></div>`;
  }

  if (gate.fallas_medias && gate.fallas_medias.length > 0) {
    html += `<div style="margin-top: 8px;"><strong>Fallas a revisar (${gate.fallas_medias.length}):</strong><ul>`;
    gate.fallas_medias.forEach(f => {
      html += `<li><strong>#${f.numero}</strong> [${f.tipo}] ${f.entrada} — <em>${f.resultado_esperado}</em> (${f.linea_o_funcion})</li>`;
    });
    html += `</ul></div>`;
  }

  if (gate.total_casos === 0) {
    html += `<div style="margin-top: 6px; opacity: 0.75;">No se encontró una Matriz de Casos de Prueba en la respuesta del modelo — este veredicto no pudo evaluarse.</div>`;
  }

  // danger_flags: hallazgos DETERMINÍSTICOS (no interpretación del LLM) —
  // ej. DELETE/UPDATE sin WHERE, host de producción detectado. Se muestran
  // aquí también (no solo en el tab Impacto) porque son la señal más
  // confiable de riesgo real y antes quedaban invisibles para quien solo
  // revisa esta pestaña.
  if (gate.danger_flags && gate.danger_flags.length > 0) {
    html += `<div style="margin-top: 10px; padding-top: 10px; border-top: 1px solid rgba(255,255,255,0.08);">`;
    html += `<strong>🔒 Hallazgos verificados en el código (${gate.danger_flags.length}):</strong><ul>`;
    gate.danger_flags.forEach(f => {
      html += `<li>${f}</li>`;
    });
    html += `</ul></div>`;
  }

  gateBanner.innerHTML = html;
}

// Extract a ```mermaid ... ``` block from the agent's markdown response and
// render it with Mermaid.js. If no block is found, or Mermaid fails to parse
// it (the model can occasionally produce invalid syntax), show a graceful
// fallback instead of a blank/broken diagram.
async function renderFlowDiagram(fullText) {
  const match = fullText.match(/```mermaid\s*\n([\s\S]*?)```/);

  if (!match) {
    flowDiagramEmpty.classList.remove('hidden');
    flowDiagramEmpty.innerHTML = '<p>El modelo no generó un diagrama de flujo en este análisis.</p>';
    flowDiagramContainer.classList.add('hidden');
    return;
  }

  const mermaidCode = match[1].trim();
  flowDiagramEmpty.classList.add('hidden');
  flowDiagramContainer.classList.remove('hidden');

  if (typeof mermaid === 'undefined') {
    flowDiagramContainer.innerHTML = '<div class="diagram-error-banner">Mermaid.js no se cargó correctamente; no se puede renderizar el diagrama.</div>';
    return;
  }

  try {
    const renderId = 'mermaid-diagram-' + Date.now();
    const { svg } = await mermaid.render(renderId, mermaidCode);
    flowDiagramContainer.innerHTML = svg;
  } catch (err) {
    flowDiagramContainer.innerHTML = `
      <div class="diagram-error-banner">
        El modelo generó un diagrama con sintaxis Mermaid inválida y no se pudo renderizar.
        <br><br><strong>Código recibido:</strong><pre style="white-space: pre-wrap; margin-top: 8px;">${mermaidCode.replace(/</g, '&lt;')}</pre>
      </div>
    `;
  }
}

// Exec Impact and Risk Analysis
// Marca un hallazgo específico (category+line) como falso positivo
// confirmado para el archivo actual. Solo aplica a este archivo — el mismo
// patrón sigue detectándose normalmente en cualquier otro archivo del
// proyecto. Actualiza la fila visualmente en vez de re-correr todo el
// análisis, para que la confirmación sea instantánea.
async function markAsFalsePositive(category, line, listItemEl) {
  if (!selectedFile) return;
  try {
    const response = await fetch(`${BACKEND_URL}/api/qa/false-positives/mark`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        filepath: selectedFile.full_path,
        category: category,
        line: line === 'null' ? null : parseInt(line, 10),
      })
    });
    if (!response.ok) throw new Error('El servidor rechazó la marca de falso positivo.');

    listItemEl.classList.add('danger-flag-marked-fp');
    const btn = listItemEl.querySelector('.btn-mark-fp');
    if (btn) {
      btn.disabled = true;
      btn.innerHTML = '<i class="fa-solid fa-check"></i> Marcado como falso positivo';
    }
    // El danger_score visible en los widgets queda desactualizado hasta la
    // próxima corrida — se lo indicamos al usuario en vez de fingir que ya
    // se recalculó, para no dar una falsa sensación de precisión inmediata.
    const note = document.createElement('div');
    note.className = 'fp-note';
    note.textContent = 'Se silenciará en la próxima vez que corras el análisis de impacto.';
    listItemEl.appendChild(note);
  } catch (error) {
    alert('No se pudo marcar como falso positivo: ' + error.message);
  }
}

// Exporta los hallazgos del archivo actual en formato SARIF 2.1.0 y
// dispara la descarga del navegador. Mismo endpoint que se puede llamar
// directamente desde CI/CD (GET /api/qa/sarif?filepath=...), así que este
// botón no duplica ninguna lógica — solo llama y descarga.
async function exportSarif() {
  if (!selectedFile) return;
  try {
    const url = `${BACKEND_URL}/api/qa/sarif?filepath=${encodeURIComponent(selectedFile.full_path)}`;
    const response = await fetch(url);
    if (!response.ok) throw new Error('El servidor no pudo generar el SARIF.');
    const sarifJson = await response.json();

    const blob = new Blob([JSON.stringify(sarifJson, null, 2)], { type: 'application/json' });
    const downloadUrl = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = downloadUrl;
    a.download = `${selectedFile.name}.sarif`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(downloadUrl);
  } catch (error) {
    alert('No se pudo exportar el SARIF: ' + error.message);
  }
}

async function runImpactAnalysis() {
  if (!selectedFile) return;
  
  impactBadge.className = 'tab-badge dot running';
  
  try {
    const response = await fetch(`${BACKEND_URL}/api/qa/impact`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filepath: selectedFile.full_path })
    });
    
    if (!response.ok) {
      throw new Error('Error al calcular el impacto del archivo.');
    }
    
    const data = await response.json();
    
    // 1. Update Widgets
    riskIndexValue.textContent = data.risk_index + '%';
    riskProgress.style.width = data.risk_index + '%';
    
    // Set risk color/rating
    let ratingColor = 'var(--accent-emerald)';
    let ratingName = 'Bajo';
    if (data.risk_index >= 90) {
      ratingColor = 'var(--accent-coral)';
      ratingName = 'CRÍTICO';
    } else if (data.risk_index >= 65) {
      ratingColor = 'var(--accent-coral)';
      ratingName = 'Alto';
    } else if (data.risk_index >= 30) {
      ratingColor = 'var(--accent-amber)';
      ratingName = 'Medio';
    }
    riskRating.innerHTML = `Calificación: <span style="color: ${ratingColor}; font-weight: 700;">${ratingName}</span>`;
    updateGlobalRiskChip(riskIndexToGateStatus(data.risk_index), 'impact');
    
    impactLevelValue.textContent = data.impact_level;
    impactCount.textContent = `${data.impact_set.length} archivos afectados directamente`;
    
    dangerScoreValue.textContent = data.danger_score + '%';
    dangerCount.textContent = `${data.danger_flags.length} banderas detectadas`;
    
    // 2. Render lists
    // Danger flags: los hallazgos con category+line real (danger_flags_structured,
    // vienen del AST) se muestran con botón "marcar falso positivo". Los que
    // solo existen como texto plano (regex sin línea, ej. host de producción,
    // credenciales) se muestran sin ese botón — no hay a qué línea apuntar.
    dangerFlagsList.innerHTML = '';
    const structuredMessages = new Set((data.danger_flags_structured || []).map(f => f.message));
    const plainOnlyFlags = data.danger_flags.filter(f => {
      // El mensaje del regex trae un prefijo distinto al de la version
      // estructurada ("CRÍTICO (verificado..." envuelve el mismo message),
      // así que comparamos por inclusión, no igualdad exacta.
      return ![...structuredMessages].some(m => f.includes(m));
    });

    if (data.danger_flags.length === 0) {
      dangerFlagsList.innerHTML = '<li class="empty-list"><i class="fa-solid fa-circle-check" style="margin-right: 6px;"></i> Ningún peligro lógico detectado.</li>';
    } else {
      (data.danger_flags_structured || []).forEach(finding => {
        const li = document.createElement('li');
        li.className = 'danger-flag-structured';
        li.innerHTML = `
          <div class="danger-flag-text"><i class="fa-solid fa-triangle-exclamation" style="margin-right: 6px;"></i> ${finding.message}</div>
          <button class="btn-mark-fp" data-category="${finding.category}" data-line="${finding.line}" title="Marcar como falso positivo para este archivo">
            <i class="fa-solid fa-flag"></i> Marcar como falso positivo
          </button>
        `;
        li.querySelector('.btn-mark-fp').addEventListener('click', (e) => {
          markAsFalsePositive(finding.category, finding.line, li);
        });
        dangerFlagsList.appendChild(li);
      });
      plainOnlyFlags.forEach(flag => {
        const li = document.createElement('li');
        li.innerHTML = `<i class="fa-solid fa-triangle-exclamation" style="margin-right: 6px;"></i> ${flag}`;
        dangerFlagsList.appendChild(li);
      });
    }
    
    // Includes
    includesList.innerHTML = '';
    if (data.includes.length === 0) {
      includesList.innerHTML = '<li class="empty-list">No incluye a otros archivos.</li>';
    } else {
      data.includes.forEach(inc => {
        const li = document.createElement('li');
        li.textContent = inc;
        includesList.appendChild(li);
      });
    }
    
    // Included by direct
    includedByDirectList.innerHTML = '';
    if (data.included_by_direct.length === 0) {
      includedByDirectList.innerHTML = '<li class="empty-list">Ningún archivo lo incluye directamente.</li>';
    } else {
      data.included_by_direct.forEach(inc => {
        const li = document.createElement('li');
        li.textContent = inc;
        includedByDirectList.appendChild(li);
      });
    }
    
    // Direct impact set (nivel 1, no transitivo — ver ajuste de main.py)
    impactSetList.innerHTML = '';
    if (data.impact_set.length === 0) {
      impactSetList.innerHTML = '<li class="empty-list">Aislado. Ningún archivo se verá afectado directamente.</li>';
    } else {
      data.impact_set.forEach(inc => {
        const li = document.createElement('li');
        li.textContent = inc;
        impactSetList.appendChild(li);
      });
    }

    // Table-sharing impact (files that share a DB table with zero include relationship)
    const tableImpactList = document.getElementById('table-impact-list');
    tableImpactList.innerHTML = '';
    if (!data.table_impact || data.table_impact.length === 0) {
      tableImpactList.innerHTML = '<li class="empty-list">Sin tablas compartidas detectadas.</li>';
    } else {
      data.table_impact.forEach(entry => {
        const li = document.createElement('li');
        li.className = 'table-impact-item';
        const filesPreview = entry.files.slice(0, 4).join(', ') + (entry.files.length > 4 ? ` (+${entry.files.length - 4} más)` : '');
        li.innerHTML = `<i class="fa-solid fa-database" style="margin-right: 6px; color: var(--accent-blue);"></i><strong>${entry.table}</strong> — ${entry.files.length} archivo(s): ${filesPreview}`;
        tableImpactList.appendChild(li);
      });
    }

    if (data.ambiguous_includes && data.ambiguous_includes.length > 0) {
      const li = document.createElement('li');
      li.className = 'empty-list';
      li.style.color = 'var(--accent-amber)';
      li.innerHTML = `<i class="fa-solid fa-triangle-exclamation" style="margin-right: 6px;"></i> Nombres ambiguos (mismo archivo en varias carpetas, se usó la primera coincidencia): ${data.ambiguous_includes.join(', ')}`;
      tableImpactList.appendChild(li);
    }

    // Visual node-edge graph
    renderDependencyGraph(data.graph);

    // Update badge dot
    if (data.risk_index >= 65) {
      impactBadge.className = 'tab-badge dot error';
    } else if (data.risk_index >= 30) {
      impactBadge.className = 'tab-badge dot running';
    } else {
      impactBadge.className = 'tab-badge dot success';
    }
    
  } catch (error) {
    impactBadge.className = 'tab-badge dot error';
    console.error(error);
  }
}

// Render the visual node-edge dependency graph using D3's force simulation.
// Nodes repel each other and edges act as springs, so the layout separates
// automatically as the graph grows — no manual arc math needed. Supports
// drag-to-reposition and scroll/pinch zoom.
let _graphSimulation = null; // keep a reference so re-renders can stop the old one

function renderDependencyGraph(graph) {
  const emptyState = document.getElementById('graph-empty-state');
  const container = document.getElementById('graph-svg-container');
  const legend = document.getElementById('graph-legend');

  // Stop any previous simulation so it doesn't keep ticking in the background
  if (_graphSimulation) {
    _graphSimulation.stop();
    _graphSimulation = null;
  }

  if (!graph || !graph.nodes || graph.nodes.length <= 1 || typeof d3 === 'undefined') {
    emptyState.classList.remove('hidden');
    container.classList.add('hidden');
    legend.classList.add('hidden');
    container.innerHTML = '';
    return;
  }

  emptyState.classList.add('hidden');
  container.classList.remove('hidden');
  legend.classList.remove('hidden');
  container.innerHTML = '';

  const width = container.clientWidth || 680;
  const height = Math.max(320, Math.min(560, 70 * graph.nodes.length));

  // D3 mutates node/edge objects in place (adds x/y/vx/vy), so work on copies
  // to avoid surprising the caller if the same `graph` object is reused.
  const nodes = graph.nodes.map(n => ({ ...n }));
  const nodeById = new Map(nodes.map(n => [n.id, n]));
  const links = (graph.edges || [])
    .filter(e => nodeById.has(e.from) && nodeById.has(e.to))
    .map(e => ({ source: e.from, target: e.to, kind: e.kind }));

  const colorFor = (type) => {
    switch (type) {
      case 'self': return 'var(--accent-purple)';
      case 'include': return 'var(--accent-emerald)';
      case 'included_by': return 'var(--accent-emerald)';
      case 'table': return 'var(--accent-blue)';
      case 'table_sibling': return 'var(--accent-blue)';
      default: return 'var(--text-muted)';
    }
  };
  const radiusFor = (type) => type === 'self' ? 10 : type === 'table' ? 7 : 6;
  const shortLabel = (node) => {
    if (node.type === 'table') return node.label || node.id.replace('table:', '');
    const parts = node.id.split('/');
    return parts[parts.length - 1];
  };

  const svg = d3.select(container)
    .append('svg')
    .attr('viewBox', [0, 0, width, height])
    .attr('width', '100%')
    .attr('height', height);

  // A <g> wrapper is what zoom/pan actually transforms; the svg itself stays fixed.
  const zoomLayer = svg.append('g');

  svg.call(
    d3.zoom()
      .scaleExtent([0.4, 3])
      .on('zoom', (event) => zoomLayer.attr('transform', event.transform))
  );

  const linkSelection = zoomLayer.append('g')
    .selectAll('line')
    .data(links)
    .join('line')
    .attr('class', d => d.kind === 'table' ? 'graph-edge table-edge' : 'graph-edge');

  const nodeGroup = zoomLayer.append('g')
    .selectAll('g')
    .data(nodes)
    .join('g')
    .call(
      d3.drag()
        .on('start', (event, d) => {
          if (!event.active) simulation.alphaTarget(0.3).restart();
          d.fx = d.x; d.fy = d.y;
        })
        .on('drag', (event, d) => { d.fx = event.x; d.fy = event.y; })
        .on('end', (event, d) => {
          if (!event.active) simulation.alphaTarget(0);
          d.fx = null; d.fy = null;
        })
    );

  nodeGroup.append('circle')
    .attr('r', d => radiusFor(d.type))
    .attr('fill', d => colorFor(d.type))
    .attr('stroke', 'rgba(0,0,0,0.3)')
    .attr('stroke-width', 1)
    .style('cursor', 'grab');

  nodeGroup.append('text')
    .attr('class', d => d.type === 'table' ? 'graph-node-label table-label' : 'graph-node-label')
    .attr('text-anchor', 'middle')
    .attr('dy', d => -radiusFor(d.type) - 6)
    .text(d => shortLabel(d));

  nodeGroup.append('title').text(d => d.id); // native tooltip with the full path on hover

  const simulation = d3.forceSimulation(nodes)
    .force('link', d3.forceLink(links).id(d => d.id).distance(d => d.kind === 'table' ? 90 : 70).strength(0.7))
    .force('charge', d3.forceManyBody().strength(-220))
    .force('center', d3.forceCenter(width / 2, height / 2))
    .force('collision', d3.forceCollide().radius(d => radiusFor(d.type) + 24))
    .on('tick', () => {
      linkSelection
        .attr('x1', d => d.source.x).attr('y1', d => d.source.y)
        .attr('x2', d => d.target.x).attr('y2', d => d.target.y);
      nodeGroup.attr('transform', d => `translate(${d.x},${d.y})`);
    });

  _graphSimulation = simulation;
}

// Orchestrator: Run all checks sequentially
async function runFullOrchestratedQA() {
  if (!selectedFile) return;
  
  // Step 1: Run Syntax check
  switchTab('tab-syntax');
  const syntaxOk = await runSyntaxCheck();
  
  // Step 2: Run Impact & Risk Analysis
  await runImpactAnalysis();
  
  if (!syntaxOk) {
    alert('Orquestador QA Detenido: Se detectaron fallos de sintaxis. Corrige los errores antes de realizar la prueba de escritorio.');
    return;
  }
  
  // Step 3: Run Ollama Desktop simulation
  switchTab('tab-desktop');
  await runDesktopTest();
}
