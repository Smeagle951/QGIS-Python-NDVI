# Exportar KML com nomes e áreas

Em **Exportar Projeto FortSmart**, marque **KML (.kml) — nomes e áreas** junto de GeoPackage, Shapefile e GeoJSON, ou use somente KML.

Use **Cada camada → arquivos com nome do QGIS** para exportar quaisquer camadas vetoriais visíveis ou selecionadas no painel. Os modos de arquivos padronizados também aceitam KML. Todas as camadas são reunidas no arquivo `fortsmart_projeto.kml`, organizadas em pastas internas com o nome de cada camada.

Polígonos recebem nome e área calculada em hectares por feição. O KML inclui uma pasta de feições e outra de rótulos pontuais para mostrar os nomes e áreas no Google Earth. Desmarque a pasta **Nomes e áreas** no visualizador se quiser ocultar esses rótulos. Pontos e linhas recebem nomes, sem área.

Os nomes vêm dos atributos nome/name, nome_talhao/nome_pivo, talhao/pivo, fieldname/blockname ou label, nesta prioridade, ignorando campos vazios. Sem nome, usa o nome da camada e o ID quando houver várias feições. Edições de apresentação feitas em outros plugins não substituem esses atributos.

As áreas são medidas com o CRS da origem e o elipsoide do projeto (WGS84 se não houver elipsoide definido). As coordenadas de saída usam WGS84. Recortes internos e geometrias multipartes são preservados; as feições originais não são editadas. Polígonos inválidos, geometrias vazias ou CRS ausente interrompem o KML daquela camada com mensagem de erro. O destino KML anterior só é substituído após concluir sua geração.

Teste: executar `scripts/test_kml_export.py` com o Python do QGIS. Após atualizar os arquivos do plugin instalado, reinicie o QGIS para carregar a opção.

## Shapefile separado ou unificado

Ao marcar **Shapefile (.shp)**, escolha **Separados** para manter um conjunto SHP por camada ou **Unificados** para juntar as feições. Como o formato Shapefile não aceita geometrias diferentes no mesmo arquivo, a opção unificada cria até três conjuntos: `fortsmart_poligonos.shp`, `fortsmart_linhas.shp` e `fortsmart_pontos.shp`. Cada registro mantém os campos `camada`, `nome`, `id_origem` e, para polígonos, `area_ha`.
