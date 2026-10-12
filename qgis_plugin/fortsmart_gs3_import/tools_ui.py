"""FortSmart — ferramentas: round-trip, calibracao USB, copia FAT32."""

from __future__ import annotations

from pathlib import Path

from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .roundtrip_core import checklist_gs3, checklist_isoxml, run_roundtrip
from .usb_calibrate import calibrate_usb, save_calibration_report
from .usb_copy import copy_payload_to_usb, detect_payload, list_usb_drives, verify_copy


OUTPUT_ROOT = Path(__file__).resolve().parent.parent.parent / "data" / "output"


class ToolsDialog(QDialog):
    def __init__(self, iface, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.setWindowTitle("FortSmart — Ferramentas (validacao / USB / Gen4)")
        self.setMinimumSize(720, 560)

        tabs = QTabWidget()
        tabs.addTab(self._build_roundtrip_tab(), "1. Round-trip")
        tabs.addTab(self._build_calibrate_tab(), "2. Calibrar USB")
        tabs.addTab(self._build_copy_tab(), "3. Copiar USB")
        tabs.addTab(self._build_help_tab(), "4. Checklists")

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)

        layout = QVBoxLayout(self)
        layout.addWidget(tabs)
        layout.addWidget(buttons)

    def _build_roundtrip_tab(self) -> QWidget:
        w = QWidget()
        self.rt_profile = QComboBox()
        for pid, label in [
            ("john_deere_gs3", "John Deere GS3 2630"),
            ("isoxml_v4", "ISOXML v4.3"),
            ("isoxml_v3", "ISOXML v3"),
            ("jacto", "Jacto OMNI"),
            ("stara", "Stara DADOS"),
            ("john_deere_gen4", "John Deere Gen4"),
        ]:
            self.rt_profile.addItem(label, pid)

        self.rt_source = QLineEdit(r"E:\GS3_2630")
        self.rt_out = QLineEdit(str(OUTPUT_ROOT / "roundtrip_test"))
        browse_src = QPushButton("...")
        browse_out = QPushButton("...")
        browse_src.clicked.connect(lambda: self._browse_dir(self.rt_source))
        browse_out.clicked.connect(lambda: self._browse_dir(self.rt_out))

        run_btn = QPushButton("Executar round-trip (ler → exportar → reimportar → comparar)")
        run_btn.clicked.connect(self._run_roundtrip)

        self.rt_log = QTextEdit()
        self.rt_log.setReadOnly(True)

        src_row = QHBoxLayout()
        src_row.addWidget(self.rt_source, 1)
        src_row.addWidget(browse_src)
        out_row = QHBoxLayout()
        out_row.addWidget(self.rt_out, 1)
        out_row.addWidget(browse_out)

        form = QFormLayout()
        form.addRow("Perfil", self.rt_profile)
        form.addRow("Origem (pendrive/pasta)", src_row)
        form.addRow("Pasta saida teste", out_row)

        layout = QVBoxLayout(w)
        layout.addLayout(form)
        layout.addWidget(run_btn)
        layout.addWidget(self.rt_log, 1)
        return w

    def _build_calibrate_tab(self) -> QWidget:
        w = QWidget()
        self.cal_path = QLineEdit()
        browse = QPushButton("...")
        browse.clicked.connect(lambda: self._browse_dir(self.cal_path))
        row = QHBoxLayout()
        row.addWidget(self.cal_path, 1)
        row.addWidget(browse)

        run_btn = QPushButton("Analisar estrutura do pendrive (Jacto/Stara/GS3/ISOXML/Gen4)")
        run_btn.clicked.connect(self._run_calibrate)
        self.cal_log = QTextEdit()
        self.cal_log.setReadOnly(True)

        form = QFormLayout()
        form.addRow("Pasta USB / export", row)
        layout = QVBoxLayout(w)
        layout.addLayout(form)
        layout.addWidget(run_btn)
        layout.addWidget(QLabel("Use quando tiver pendrive real Jacto/Stara para comparar nomes de pastas."))
        layout.addWidget(self.cal_log, 1)
        return w

    def _build_copy_tab(self) -> QWidget:
        w = QWidget()
        self.copy_src = QLineEdit(str(OUTPUT_ROOT))
        browse = QPushButton("...")
        browse.clicked.connect(lambda: self._browse_dir(self.copy_src))
        src_row = QHBoxLayout()
        src_row.addWidget(self.copy_src, 1)
        src_row.addWidget(browse)

        self.usb_combo = QComboBox()
        refresh = QPushButton("Atualizar unidades")
        refresh.clicked.connect(self._refresh_usbs)
        usb_row = QHBoxLayout()
        usb_row.addWidget(self.usb_combo, 1)
        usb_row.addWidget(refresh)

        self.overwrite = QCheckBox("Sobrescrever pasta existente no USB")
        self.overwrite.setChecked(True)
        self.require_fat32 = QCheckBox("Exigir FAT32 (recomendado GS3)")
        self.require_fat32.setChecked(True)

        run_btn = QPushButton("Copiar payload para USB")
        run_btn.clicked.connect(self._run_copy)
        self.copy_log = QTextEdit()
        self.copy_log.setReadOnly(True)

        form = QFormLayout()
        form.addRow("Pasta export FortSmart", src_row)
        form.addRow("Unidade USB", usb_row)
        form.addRow("", self.overwrite)
        form.addRow("", self.require_fat32)

        layout = QVBoxLayout(w)
        layout.addLayout(form)
        layout.addWidget(run_btn)
        layout.addWidget(self.copy_log, 1)
        self._refresh_usbs()
        return w

    def _build_help_tab(self) -> QWidget:
        w = QWidget()
        text = QTextEdit()
        text.setReadOnly(True)
        text.setPlainText(
            checklist_gs3()
            + "\n\n"
            + checklist_isoxml()
            + "\n\n"
            + "Gen4 Deere\n"
            + "1. Exportar FortSmart → John Deere Gen4\n"
            + "2. Resultado: Gen4/MasterData.xml + SpatialFiles/*.gjson\n"
            + "3. Copiar pasta Gen4 para USB (wizard)\n"
            + "4. Importar no display Gen4 / software compativel\n"
            + "Nota: GS3 2630 NAO le Gen4 direto — use perfil GS3 para o 2630.\n"
        )
        layout = QVBoxLayout(w)
        layout.addWidget(text)
        return w

    def _browse_dir(self, edit: QLineEdit) -> None:
        path = QFileDialog.getExistingDirectory(self, "Selecionar pasta", edit.text())
        if path:
            edit.setText(path)

    def _run_roundtrip(self) -> None:
        try:
            profile = self.rt_profile.currentData()
            report = run_roundtrip(
                Path(self.rt_source.text().strip()),
                profile_id=profile,
                output_root=Path(self.rt_out.text().strip()),
                keep_export=True,
                template_gs3=Path(self.rt_source.text().strip()) if profile == "john_deere_gs3" else None,
            )
            self.rt_log.setPlainText(report.summary_text())
            icon = QMessageBox.Information if report.ok else QMessageBox.Warning
            QMessageBox(icon, "Round-trip", "OK" if report.ok else "Com divergencias — veja o log.").exec_()
        except Exception as exc:
            self.rt_log.setPlainText(str(exc))
            QMessageBox.critical(self, "Round-trip", str(exc))

    def _run_calibrate(self) -> None:
        try:
            root = Path(self.cal_path.text().strip())
            report = calibrate_usb(root)
            out = OUTPUT_ROOT / "usb_calibration"
            save_calibration_report(report, out)
            self.cal_log.setPlainText(report.summary_text() + f"\n\nRelatorio salvo em:\n{out}")
        except Exception as exc:
            self.cal_log.setPlainText(str(exc))
            QMessageBox.critical(self, "Calibracao", str(exc))

    def _refresh_usbs(self) -> None:
        self.usb_combo.clear()
        drives = list_usb_drives()
        if not drives:
            self.usb_combo.addItem("(nenhuma unidade encontrada)", "")
            return
        for d in drives:
            label = f"{d.letter}: [{d.file_system or '?'}] {d.label} — {d.free_gb:.1f} GB livres"
            if d.fat32_ok:
                label += " ✓FAT32"
            self.usb_combo.addItem(label, d.letter)

    def _run_copy(self) -> None:
        try:
            src = Path(self.copy_src.text().strip())
            letter = self.usb_combo.currentData()
            if not letter:
                raise RuntimeError("Selecione uma unidade USB.")
            usb = Path(f"{letter}:\\")
            profile, payload, hint = detect_payload(src)
            drives = {d.letter: d for d in list_usb_drives()}
            drive = drives.get(letter)
            if self.require_fat32.isChecked() and drive and not drive.fat32_ok:
                raise RuntimeError(
                    f"Unidade {letter}: esta em {drive.file_system}, nao FAT32.\n"
                    "GS3 exige FAT32. Desmarque a opcao so se souber o que esta fazendo."
                )
            dest = copy_payload_to_usb(src, usb, overwrite=self.overwrite.isChecked())
            issues = verify_copy(dest)
            msg = f"Perfil detectado: {profile}\n{hint}\n\nCopiado para:\n{dest}\n"
            if issues:
                msg += "\nAvisos:\n- " + "\n- ".join(issues)
            else:
                msg += "\nVerificacao: OK"
            self.copy_log.setPlainText(msg)
            QMessageBox.information(self, "Copia USB", msg)
        except Exception as exc:
            self.copy_log.setPlainText(str(exc))
            QMessageBox.critical(self, "Copia USB", str(exc))


def run_tools(iface) -> None:
    dlg = ToolsDialog(iface)
    dlg.exec_()
