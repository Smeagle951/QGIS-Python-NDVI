# Exportar QGIS → pendrive John Deere GS3 2630

Guia FortSmart + LK-Technik Path Planner.

---

## Visao geral

| O que enviar | Ferramenta | Formato no pendrive |
|--------------|------------|---------------------|
| **Limites / talhoes** | **FortSmart GS3 Export** (plugin) | `GS3_2630\JD4600\RCD\EIC\...` |
| **Linhas de guia (AB, curvas)** | **LK-Technik Path Planner** | ISOXML v3 (`TASKDATA\TASKDATA.XML`) ou Gen4 |

O GS3 2630 **nao le Gen4 direto** no pendrive. Para o monitor GS3, limites sao formato **RCD/fdShape** (FortSmart).

---

## Parte 1 — Limites/talhoes (FortSmart)

### 1. Instalar/atualizar plugin

```powershell
powershell -ExecutionPolicy Bypass -File "C:\Users\fortu\Desktop\QGIS_Python_NDVI\scripts\install_fortsmart_gs3_plugin.ps1"
```

Reinicie o QGIS e ative **FortSmart GS3 Import/Export** em Complementos → Instalados.

### 2. Preparar camada no QGIS

Use a camada importada:

- `FortSmart Talhoes GS3 (todos — 74)`

Ou qualquer camada de **poligonos** com colunas `cliente`, `fazenda`, `talhao` (opcional).

### 3. Exportar

1. Clique **Exportar para GS3 2630** (barra de ferramentas ou Complementos → FortSmart)
2. **Camada de talhoes:** selecione a camada
3. **Pasta de saida:** ex. `C:\Users\fortu\Desktop\QGIS_Python_NDVI\data\output\export_gs3_usb`
4. **Template GS3:** `E:\GS3_2630` (recomendado — copia `host`, `global.ver`, mescla `setup.fds`)
5. Marque **Usar pendrive/template GS3 existente**
6. **OK**

Sera criada a pasta:

```
export_gs3_usb\GS3_2630\JD4600\RCD\EIC\
  setup.fds
  host
  global.ver
  Fields\{XX}\{guid}\Boundary....fdShape
```

### 4. Copiar para o pendrive

1. Pendrive **FAT32**
2. Copie **somente** a pasta `GS3_2630` para a **raiz** do USB  
   Resultado: `F:\GS3_2630\JD4600\RCD\...`
3. **Nao** coloque ZIP na raiz do pendrive

### 5. Importar no monitor GS3 2630

1. Pare gravacao / pare o trator
2. Insira o USB (com sistema ligado)
3. **Data Transfer** → **Import Data**
4. Selecione o perfil ou confirme importacao
5. Aguarde **Data transfer is complete**
6. Em **Fields**, verifique os talhoes

---

## Parte 2 — Linhas de guia (LK-Technik Path Planner)

O LK-Technik **nao lista GS3 2630** no terminal. Use:

- **ISOXML v3** — mais compativel com ecossistema Deere antigo
- **John Deere Gen4** — so se voce tiver display Gen4 (nao GS3)

### 1. Instalar LK-Technik no QGIS

1. Complementos → Gerenciar e instalar complementos
2. Instale **LK-Technik Path Planner**  
   Ou copie de:  
   `C:\Users\fortu\Downloads\lk_technik_path_planner-main\lk_technik_path_planner-main`

### 2. Estrutura no QGIS (LK-Technik)

O plugin espera grupos:

```
Cliente (Kunde)
  └── Fazenda (Betrieb)
        ├── Feldgrenzen  (limites)
        ├── Fahrspuren   (linhas de guia)
        └── Felder.csv   (catalogo)
```

**Atalho:** importe talhoes com FortSmart GS3 Import; desenhe **Fahrspuren** manualmente ou importe ISOXML.

### 3. Exportar linhas ISOXML v3

1. Abra **LK-Technik Path Planner**
2. Modo **Export**
3. Terminal: escolha **ISOXML v3** (nao Gen4 para GS3)
4. Selecione cliente/fazenda/talhoes na arvore
5. Pasta de saida: ex. `D:\export_lk_isoxml`
6. **Executar**

Gera:

```
export_lk_isoxml\TASKDATA\TASKDATA.XML
```

### 4. GS3 e ISOXML v3

O GS3 2630 **nao importa TASKDATA.XML direto** como Gen4.

Opcoes:

| Opcao | Como |
|-------|------|
| **Apex / Data Manager Deere** (desktop) | Importa ISOXML → exporta cartao GS3 2630 |
| **Somente limites** | Use FortSmart Export (Parte 1) — funciona direto |
| **Display Gen4** | LK-Technik → export Gen4 |

Se voce **nao tem Apex**, use FortSmart para **limites** e desenhe guias **no proprio monitor** a partir dos limites importados.

---

## Parte 3 — Fluxo completo recomendado (Faz. Ferradura)

```
Pendrive E:\GS3_2630
    ↓ FortSmart Import
QGIS (74 talhoes + NDVI FortSmart)
    ↓ editar limites se necessario
    ↓ FortSmart Export + template E:\GS3_2630
Pasta GS3_2630 nova no PC
    ↓ copiar para USB FAT32
Monitor GS3 2630 ← Import Data
```

Linhas AB (opcional):

```
QGIS Fahrspuren (LK-Technik)
    ↓ Export ISOXML v3
Apex / Data Manager (se disponivel)
    ↓ export GS3 card
Monitor GS3
```

---

## Erros comuns

| Problema | Solucao |
|----------|---------|
| Monitor nao ve talhoes | Pasta deve ser `USB:\GS3_2630\` na raiz, FAT32 |
| Só 2 talhoes no QGIS | Atualize plugin v1.0.1+ e reimporte |
| LK-Technik nao exporta GS3 | Normal — use FortSmart Export para limites |
| Gen4 no GS3 nao funciona | GS3 usa RCD, nao MasterData.xml Gen4 |
| Perdeu linhas antigas no USB | Marque template `E:\GS3_2630` para mesclar setup.fds |

---

## Comandos uteis

Reinstalar plugin FortSmart:

```powershell
powershell -ExecutionPolicy Bypass -File "C:\Users\fortu\Desktop\QGIS_Python_NDVI\scripts\install_fortsmart_gs3_plugin.ps1"
```

Export CLI de teste (talhoes do GPKG):

```powershell
python C:\Users\fortu\Desktop\QGIS_Python_NDVI\scripts\gs3_rcd_to_gpkg.py --input E:\GS3_2630
```

---

## Suporte FortSmart

- Import: botao **Importar GS3 2630**
- Export: botao **Exportar para GS3 2630**
- NDVI: `scripts/qgis_process_indices.py` (Console Python QGIS)
