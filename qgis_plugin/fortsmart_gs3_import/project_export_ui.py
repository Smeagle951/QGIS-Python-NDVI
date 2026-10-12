"""FortSmart — dialogo Exportar Projeto (lote GPKG + SHP)."""

from __future__ import annotations

from pathlib import Path

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .dialog_scroll import build_scroll_dialog, fit_dialog_to_screen
from .export_destination_ui import ExportDestinationPanel, project_export_tree_lines
from .field_group_export import choose_export_fields, export_name_map
from .project_batch_exporter import (
    EXPORT_LAYERS,
    ExportResult,
    default_project_folder_name,
    export_active_layers_direct,
    export_project_batch,
    validate_project,
)
from .qgis_layer_source import LayerSourceMode, resolve_source_layers, sanitize_filename, summarize_active_layers


OUTPUT_ROOT = Path(__file__).resolve().parent.parent.parent / "data" / "output"


class ProjectExportDialog(QDialog):
    def __init__(self, iface, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.setWindowTitle("Exportar Projeto FortSmart")
        fit_dialog_to_screen(self, width=520, height=500, min_width=460, min_height=360)

        title = QLabel(
            "<b>Exportar camadas do QGIS</b> — visiveis ou selecionadas no painel Camadas."
        )
        title.setWordWrap(True)
        title.setTextFormat(Qt.RichText)

        # --- Origem (camadas QGIS) ---
        src_box = QGroupBox("Origem — camadas do QGIS")
        self.scope_group = QButtonGroup(self)
        self.rb_visible = QRadioButton("Visiveis no painel")
        self.rb_selected = QRadioButton("Selecionadas (Ctrl+clique)")
        self.rb_visible.setChecked(True)
        self.scope_group.addButton(self.rb_visible, 0)
        self.scope_group.addButton(self.rb_selected, 1)

        self.map_group = QButtonGroup(self)
        self.rb_auto = QRadioButton("Auto → arquivos padrao 01…13")
        self.rb_direct = QRadioButton("Cada camada → arquivos com nome do QGIS")
        self.rb_exact = QRadioButton("Nomes exatos 01_Limite_Externo…")
        self.rb_direct.setChecked(True)
        self.map_group.addButton(self.rb_auto, 0)
        self.map_group.addButton(self.rb_direct, 1)
        self.map_group.addButton(self.rb_exact, 2)

        src_layout = QVBoxLayout(src_box)
        src_layout.addWidget(self.rb_visible)
        src_layout.addWidget(self.rb_selected)
        src_layout.addWidget(self.rb_auto)
        src_layout.addWidget(self.rb_direct)
        src_layout.addWidget(self.rb_exact)

        hint = QLabel("<i>AdaptiveCurve, Limite, LINHAS, LIMITE BOR</i> reconhecidos no modo auto.")
        hint.setWordWrap(True)
        hint.setTextFormat(Qt.RichText)
        src_layout.addWidget(hint)

        self.scope_group.buttonClicked.connect(self._on_source_changed)
        self.map_group.buttonClicked.connect(self._on_source_changed)

        # --- Destino ---
        self.destination = ExportDestinationPanel(
            title="Onde salvar",
            default_parent=OUTPUT_ROOT,
            default_folder_name=default_project_folder_name(),
        )
        self.destination.path_changed.connect(self._refresh_destination_tree)

        # --- Formato ---
        fmt_box = QGroupBox("Formato")
        self.fmt_gpkg = QCheckBox("GeoPackage (.gpkg)")
        self.fmt_gpkg.setChecked(True)
        self.fmt_shp = QCheckBox("Shapefile (.shp)")
        self.fmt_shp.setChecked(True)
        self.shp_mode_box = QGroupBox("Organização do Shapefile")
        self.shp_mode_group = QButtonGroup(self)
        self.shp_separate = QRadioButton("Separados — um SHP para cada camada")
        self.shp_unified = QRadioButton("Unificados — um SHP para polígonos, linhas e pontos")
        self.shp_separate.setChecked(True)
        self.shp_mode_group.addButton(self.shp_separate)
        self.shp_mode_group.addButton(self.shp_unified)
        shp_mode_layout = QVBoxLayout(self.shp_mode_box)
        shp_mode_layout.addWidget(self.shp_separate)
        shp_mode_layout.addWidget(self.shp_unified)
        self.fmt_geojson = QCheckBox("GeoJSON")
        self.fmt_kml = QCheckBox("KML (.kml) — nomes e áreas")
        self.fmt_kml.setToolTip("Um KML por camada. Polígonos com nome e hectares; pontos e linhas com nome. Coordenadas WGS84.")
        fmt_layout = QVBoxLayout(fmt_box)
        fmt_layout.addWidget(self.fmt_gpkg)
        fmt_layout.addWidget(self.fmt_shp)
        fmt_layout.addWidget(self.shp_mode_box)
        fmt_layout.addWidget(self.fmt_geojson)
        fmt_layout.addWidget(self.fmt_kml)
        self.fmt_gpkg.stateChanged.connect(self._refresh_destination_tree)
        self.fmt_shp.stateChanged.connect(self._refresh_destination_tree)
        self.fmt_shp.stateChanged.connect(lambda: self.shp_mode_box.setVisible(self.fmt_shp.isChecked()))
        self.shp_mode_group.buttonClicked.connect(self._refresh_destination_tree)
        self.fmt_geojson.stateChanged.connect(self._refresh_destination_tree)
        self.fmt_kml.stateChanged.connect(self._refresh_destination_tree)

        # --- Camadas padrao 01-13 ---
        self.layers_box = QGroupBox("Arquivos 01…13")
        self.layer_checks: dict[int, QCheckBox] = {}
        layers_inner = QWidget()
        layers_layout = QVBoxLayout(layers_inner)
        for spec in EXPORT_LAYERS:
            cb = QCheckBox(spec["label"])
            cb.setChecked(spec.get("default_export", True))
            self.layer_checks[spec["order"]] = cb
            cb.stateChanged.connect(self._refresh_destination_tree)
            layers_layout.addWidget(cb)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(layers_inner)
        scroll.setMaximumHeight(110)
        layers_v = QVBoxLayout(self.layers_box)
        layers_v.addWidget(scroll)

        sel_row = QHBoxLayout()
        btn_all = QPushButton("Marcar todas")
        btn_none = QPushButton("Desmarcar todas")
        btn_default = QPushButton("Padrao")
        btn_all.clicked.connect(lambda: self._set_all(True))
        btn_none.clicked.connect(lambda: self._set_all(False))
        btn_default.clicked.connect(self._set_operational_default)
        sel_row.addWidget(btn_all)
        sel_row.addWidget(btn_none)
        sel_row.addWidget(btn_default)
        layers_v.addLayout(sel_row)

        refresh = QPushButton("Atualizar preview")
        refresh.clicked.connect(self._refresh_validation)

        self.validation_text = QTextEdit()
        self.validation_text.setReadOnly(True)
        self.validation_text.setMaximumHeight(100)
        self.validation_text.setMinimumHeight(64)

        buttons = QDialogButtonBox(QDialogButtonBox.Cancel | QDialogButtonBox.Ok)
        buttons.button(QDialogButtonBox.Ok).setText("Exportar")
        buttons.accepted.connect(self._on_export)
        buttons.rejected.connect(self.reject)

        content_layout = build_scroll_dialog(self, buttons)
        content_layout.addWidget(title)
        content_layout.addWidget(src_box)
        content_layout.addWidget(self.destination)
        content_layout.addWidget(fmt_box)
        content_layout.addWidget(self.layers_box)
        content_layout.addWidget(refresh)
        content_layout.addWidget(QLabel("Preview camadas QGIS"))
        content_layout.addWidget(self.validation_text)

        self._on_source_changed()
        self._refresh_destination_tree()
        self._refresh_validation()

    def _scope_mode(self) -> LayerSourceMode:
        return LayerSourceMode.SELECTED if self.rb_selected.isChecked() else LayerSourceMode.VISIBLE

    def _map_mode(self) -> LayerSourceMode:
        if self.rb_exact.isChecked():
            return LayerSourceMode.EXACT_NAMES
        return LayerSourceMode.AUTO_MAP

    def _on_source_changed(self) -> None:
        direct = self.rb_direct.isChecked()
        self.layers_box.setVisible(not direct)
        self._refresh_destination_tree()
        self._refresh_validation()

    def _selected_orders(self) -> list[int]:
        return [order for order, cb in self.layer_checks.items() if cb.isChecked()]

    def _set_all(self, checked: bool) -> None:
        for cb in self.layer_checks.values():
            cb.setChecked(checked)

    def _set_operational_default(self) -> None:
        for spec in EXPORT_LAYERS:
            self.layer_checks[spec["order"]].setChecked(spec.get("default_export", True))

    def _refresh_destination_tree(self) -> None:
        if self.rb_direct.isChecked():
            layers = resolve_source_layers(self._scope_mode())
            lines = []
            for layer in sorted(layers, key=lambda l: l.name().casefold()):
                if layer.featureCount() == 0:
                    continue
                base = layer.name().casefold().replace(" ", "_")[:40]
                parts = []
                if self.fmt_gpkg.isChecked():
                    parts.append(f"{base}.gpkg")
                if self.fmt_shp.isChecked() and self.shp_separate.isChecked():
                    parts.append(f"{base}.shp")
                if self.fmt_geojson.isChecked():
                    parts.append(f"{base}.geojson")
                if parts:
                    lines.append(f"  {layer.name()}")
                    for p in parts:
                        lines.append(f"    {p}")
            if self.fmt_kml.isChecked():
                lines.append("  fortsmart_projeto.kml (todas as camadas)")
            if self.fmt_shp.isChecked() and self.shp_unified.isChecked():
                lines.extend([
                    "  SHP unificados por tipo de geometria:",
                    "    fortsmart_poligonos.shp",
                    "    fortsmart_linhas.shp",
                    "    fortsmart_pontos.shp",
                ])
            lines.append("  fortsmart_project.json")
            self.destination.set_tree_lines(["  {Talhao selecionado}/"] + ["  " + line for line in lines])
            return

        lines = project_export_tree_lines(
            self._selected_orders(),
            self.fmt_gpkg.isChecked(),
            self.fmt_shp.isChecked(),
            self.fmt_geojson.isChecked(),
            EXPORT_LAYERS,
            export_kml=self.fmt_kml.isChecked(),
            unify_shp=self.shp_unified.isChecked(),
        )
        self.destination.set_tree_lines(["  {Talhao selecionado}/"] + ["  " + line for line in lines])

    def _refresh_validation(self) -> None:
        try:
            if self.rb_direct.isChecked():
                layers = resolve_source_layers(self._scope_mode())
                self.validation_text.setPlainText(summarize_active_layers(layers))
                return

            scope = self._scope_mode()
            map_mode = self._map_mode()
            scope_label = "visiveis" if scope == LayerSourceMode.VISIBLE else "selecionadas"
            map_label = (
                "mapeamento automatico → 01…13"
                if map_mode == LayerSourceMode.AUTO_MAP
                else "nomes exatos FortSmart"
            )

            active = summarize_active_layers(resolve_source_layers(scope))
            v = validate_project(
                self._selected_orders(),
                source_mode=map_mode,
                scope_mode=scope,
            )
            text = (
                f"ORIGEM: camadas {scope_label} no QGIS\n"
                f"MODO: {map_label}\n"
                f"{'=' * 40}\n\n"
                f"{active}\n\n"
                f"{'=' * 40}\n"
                f"MAPEAMENTO PARA EXPORT\n"
                f"{'=' * 40}\n\n"
                f"{v.summary_text()}"
            )
            self.validation_text.setPlainText(text)
        except Exception as exc:
            self.validation_text.setPlainText(f"Erro na validacao: {exc}")

    def _on_export(self) -> None:
        export_gpkg = self.fmt_gpkg.isChecked()
        export_shp = self.fmt_shp.isChecked()
        shp_mode = "unified" if self.shp_unified.isChecked() else "separate"
        export_geojson = self.fmt_geojson.isChecked()
        export_kml = self.fmt_kml.isChecked()
        if not (export_gpkg or export_shp or export_geojson or export_kml):
            QMessageBox.warning(self, "FortSmart", "Selecione ao menos um formato de saida.")
            return

        out_dir = self.destination.full_path()

        try:
            from qgis.core import QgsProject

            scope = self._scope_mode()
            layers = resolve_source_layers(scope)
            selection = choose_export_fields(self, layers, QgsProject.instance().layerTreeRoot())
            if selection is None:
                return
            groups, edited_names = selection
            field_names = export_name_map(groups, edited_names)
            result = ExportResult(output_dir=out_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            for group in groups:
                field_name = field_names[next(iter(group.layer_ids))]
                field_dir = out_dir / sanitize_filename(field_name)
                group_ids = group.layer_ids
                if self.rb_direct.isChecked():
                    part = export_active_layers_direct(
                        field_dir,
                        source_mode=scope,
                        selected_layer_ids=group_ids,
                        field_name=field_name,
                        export_gpkg=export_gpkg,
                        export_shp=export_shp,
                        shp_mode=shp_mode,
                        export_geojson=export_geojson,
                        export_kml=export_kml,
                    )
                else:
                    map_mode = self._map_mode()
                    orders = self._selected_orders()
                    if not orders:
                        QMessageBox.warning(self, "FortSmart", "Selecione ao menos um arquivo 01…13.")
                        return
                    validation = validate_project(
                        orders, source_mode=map_mode, scope_mode=scope,
                        selected_layer_ids=group_ids,
                    )
                    if validation.exportable_count == 0:
                        result.skipped.append(f"{field_name}: nenhuma camada compativel")
                        continue
                    part = export_project_batch(
                        field_dir,
                        export_gpkg=export_gpkg,
                        export_shp=export_shp,
                        shp_mode=shp_mode,
                        export_geojson=export_geojson,
                        export_kml=export_kml,
                        selected_orders=orders,
                        source_mode=map_mode,
                        scope_mode=scope,
                        selected_layer_ids=group_ids,
                        field_name=field_name,
                    )
                result.exported_files.extend(f"{field_dir.name}/{name}" for name in part.exported_files)
                result.skipped.extend(part.skipped)
                result.errors.extend(part.errors)
        except Exception as exc:
            QMessageBox.critical(self, "FortSmart", str(exc))
            return

        if result.errors:
            QMessageBox.warning(
                self,
                "FortSmart",
                f"Export concluido com avisos.\n\nPasta:\n{out_dir}\n\nErros:\n"
                + "\n".join(result.errors),
            )
        else:
            msg = (
                f"Exportado a partir das camadas do QGIS:\n\n{out_dir}\n\n"
                f"Arquivos: {len(result.exported_files)}\n"
                f"Ignorados: {len(result.skipped)}"
            )
            if result.skipped:
                msg += "\n\n" + "\n".join(f"  - {s}" for s in result.skipped[:10])
            QMessageBox.information(self, "FortSmart", msg)

        self.accept()


def run_project_export(iface) -> ExportResult | None:
    dlg = ProjectExportDialog(iface)
    if dlg.exec_() != QDialog.Accepted:
        return None
    return ExportResult(output_dir=dlg.destination.full_path())
