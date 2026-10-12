# FortSmart SendTo2020 - Relatorio de implementacao

Data: 2026-09-28  
Versao do plugin: 2.9.6  
Estado: `ROUNDTRIP_TESTED`  
Estado de hardware: `READY_FOR_DEVICE_TEST` (nao `DEVICE_VERIFIED`)

## KEEP

- `CanonicalProject`, roles canonicos e coleta QGIS existentes.
- Geometria original das camadas QGIS.
- Geradores de limites, bordaduras, linhas, obstaculos e simulacao.
- Exportadores John Deere GS3/Gen4, Jacto, Stara e ISOXML.
- Contrato MCD comprovado no pacote funcional.

## MODIFY

- `adapters/sendto2020_writer.py`: filtro estrito por `fortsmart_role`/`canonical_role`, com fallback legado apenas quando nao existe role.
- `sendto2020_export.py`: exportacao atomica por `field_id`, somente `EXTERNAL_BOUNDARY`, um `Polygon` e um SHP por talhao.
- `profiles.py`: SendTo2020 suporta somente limite e registra `external_boundary_status=ROUNDTRIP_TESTED`.
- `metadata.txt`: versao 2.9.4.

## CREATE

- `sendto2020_geometry.py`: copia temporaria, CRS metrico, simplificacao topologica adaptativa, controle de vertices e area.
- `sendto2020_preflight.py`: validacao dos arquivos e reabertura Fiona/GDAL antes da publicacao.
- `sendto2020_compare.py`: comparacao estrutural com pacote funcional, sem comparar bytes ou dados privados.
- `tests/test_sendto2020_geometry.py`: testes de complexidade, area e MultiPolygon.
- `tests/test_sendto2020_export.py`: filtro semantico, round-trip, isolamento, esquema e bloqueios.
- `scripts/validate_sendto2020_final.py`: prova reproduzivel com 461.569 vertices.

## DEPRECATE

- Agregacao de `LIMITE + BORDADURA` no mesmo shapefile SendTo2020.
- Multiplas features de boundary no mesmo SHP.
- Publicacao de pasta final antes da validacao de round-trip.

## Contrato final

Para cada talhao:

```text
1 field_id
1 EXTERNAL_BOUNDARY principal
1 shapefile
1 feature Polygon
EPSG:4326
Business/Farm/Field/CODE
SHP/SHX/DBF/PRJ/CPG/MCD
```

`Business` recebe o proprietario, `Farm` recebe a fazenda e `Field`/`CODE` recebem o talhao. `INTERNAL_BOUNDARY`, `HEADLAND`, `BORDER_PASS`, `FINAL_WORK_PATH`, `WORK_OFF` e `WORK_ON` sao registrados no plano como ignorados e nao entram no boundary.

## Validacao executada

- Suite completa: 24 testes aprovados.
- Entrada de estresse: 461.569 vertices.
- Saida: 1.026 vertices.
- Diferenca de area: 0,0002389846% (limite configurado: 0,5%).
- Geometria canonica original: inalterada.
- Round-trip: 1 `Polygon`, EPSG:4326, esquema DBF e atributos confirmados.
- Comparacao com pacote funcional: estrutura compativel.

Artefatos:

- `data/output/sendto2020_final_validation_20260928/SendTo2020`
- `data/output/sendto2020_final_validation_20260928/SENDTO2020_GOLDEN_COMPARISON.json`
- `data/output/sendto2020_final_validation_20260928/SENDTO2020_DIAGNOSTIC_EXPORT`

## Limites restantes

- O boundary esta pronto para teste fisico no monitor Precision Planting 20|20.
- O estado nao deve ser promovido para `DEVICE_VERIFIED` antes de confirmar no equipamento que o talhao aparece, pode ser selecionado e o limite e exibido.
- Linhas de guia, prescricoes e dados de trabalho nao fazem parte desta entrega e exigem contrato separado.

## Correcao USB 2.9.5

- Corrigida a deteccao de `SendTo2020` em `usb_copy.detect_payload()`.
- A pasta agora e copiada para `{USB}:\SendTo2020`, diretamente na raiz do pendrive.
- A copia e reaberta e validada no proprio USB.
- O `.mcd` passou a reproduzir o contrato da referencia sem CRLF final adicional.
- `HEADLAND` e `FIELD_HEADLAND_PATH` continuam fora do SHP porque o pacote funcional 20|20 usa apenas o limite externo do campo; a bordadura operacional nao e um objeto separado nesse contrato.

## Verificacao de midia 2.9.6

- `verify_copy()` consulta `HealthStatus` e `OperationalStatus` do volume no Windows.
- Pendrive marcado como `Warning` ou `Full Repair Needed` passa a gerar aviso visivel, mesmo quando os arquivos SendTo2020 foram copiados e reabertos corretamente.
