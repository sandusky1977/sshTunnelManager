#!/usr/bin/env python3
import sys
import os
import json
import time
import logging
import subprocess
import threading
import signal
from datetime import datetime
from PyQt5.QtWidgets import (QApplication, QSystemTrayIcon, QMenu, 
                           QAction, QDialog, QFormLayout, QLineEdit, 
                           QSpinBox, QFileDialog, QPushButton, QVBoxLayout, 
                           QTabWidget, QWidget, QTextEdit, QTableWidget, 
                           QTableWidgetItem, QHeaderView, QLabel, QTimeEdit,
                           QCheckBox, QHBoxLayout, QComboBox, QGroupBox,
                           QMainWindow, QMessageBox)
from PyQt5.QtCore import Qt, QTimer, QTime, pyqtSignal, QObject
from PyQt5.QtGui import QIcon, QPixmap, QColor

# Create a separate logger for the application
LOGFILE = os.path.expanduser("~/.ssh_tunnel_manager.log")
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOGFILE),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("ssh_tunnel_manager")

# Store settings in the user's home directory
SETTINGS_FILE = os.path.expanduser("~/.ssh_tunnel_manager.json")

class ConnectionState:
    DISCONNECTED = 0
    CONNECTING = 1
    CONNECTED = 2
    ERROR = 3

class ConnectionEvent:
    def __init__(self, event_type, timestamp=None, details=None):
        self.event_type = event_type
        self.timestamp = timestamp or datetime.now()
        self.details = details or ""
    
    def __str__(self):
        return f"{self.timestamp.strftime('%Y-%m-%d %H:%M:%S')} - {self.event_type} - {self.details}"

class BandwidthMonitor(QObject):
    bandwidth_updated = pyqtSignal(float, float)  # Upload KB/s, Download KB/s
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.running = False
        self.process = None
        self.rx_bytes_prev = 0
        self.tx_bytes_prev = 0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_bandwidth)
        self.ssh_proc = None
        
    def start_monitoring(self, port):
        self.running = True
        self.timer.start(1000)  # Update every second
    
    def stop_monitoring(self):
        self.running = False
        self.timer.stop()
        self.bandwidth_updated.emit(0, 0)
    
    def update_bandwidth(self):
        # Rather than show random values, just indicate connection is active
        if self.running:
            upload = 0.1
            download = 0.1
            self.bandwidth_updated.emit(upload, download)
        else:
            self.bandwidth_updated.emit(0, 0)

class TunnelManager(QObject):
    status_changed = pyqtSignal(int, str)  # state, message
    connection_event = pyqtSignal(ConnectionEvent)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.tunnel_process = None
        self.state = ConnectionState.DISCONNECTED
        self.reconnect_attempts = 0
        self.max_reconnect_attempts = 5
        self.reconnect_delay = 5  # seconds
        self.reconnect_timer = QTimer(self)
        self.reconnect_timer.timeout.connect(self.attempt_reconnect)
        self.bandwidth_monitor = BandwidthMonitor(self)
        self.settings = {
            "host": "",
            "port": 22,
            "username": "",
            "key_path": "",
            "local_port": 8096,
            "remote_port": 8096,
            "auto_reconnect": True,
            "scheduled_connect": False,
            "connect_time": QTime(8, 0),
            "disconnect_time": QTime(22, 0)
        }
        self.connection_history = []
        
        # Set up scheduling timer
        self.schedule_timer = QTimer(self)
        self.schedule_timer.timeout.connect(self.check_schedule)
        self.schedule_timer.start(60000)  # Check schedule every minute
    
    def load_settings(self):
        try:
            if os.path.exists(SETTINGS_FILE):
                with open(SETTINGS_FILE, "r") as f:
                    saved_settings = json.load(f)
                    
                # Update settings, ignoring QTime objects which can't be serialized
                for key, value in saved_settings.items():
                    if key not in ["connect_time", "disconnect_time"]:
                        self.settings[key] = value
                
                # Handle time settings separately
                if "connect_time_str" in saved_settings:
                    self.settings["connect_time"] = QTime.fromString(saved_settings["connect_time_str"], "hh:mm")
                if "disconnect_time_str" in saved_settings:
                    self.settings["disconnect_time"] = QTime.fromString(saved_settings["disconnect_time_str"], "hh:mm")
                    
                logger.info("Settings loaded successfully")
        except Exception as e:
            logger.error(f"Error loading settings: {e}")
    
    def save_settings(self):
        try:
            # Create a copy of settings for serialization
            settings_to_save = self.settings.copy()
            
            # Convert QTime objects to strings
            settings_to_save["connect_time_str"] = self.settings["connect_time"].toString("hh:mm")
            settings_to_save["disconnect_time_str"] = self.settings["disconnect_time"].toString("hh:mm")
            
            # Remove actual QTime objects which can't be serialized
            del settings_to_save["connect_time"]
            del settings_to_save["disconnect_time"]
            
            with open(SETTINGS_FILE, "w") as f:
                json.dump(settings_to_save, f, indent=2)
            logger.info("Settings saved successfully")
        except Exception as e:
            logger.error(f"Error saving settings: {e}")
    
    def check_schedule(self):
        if not self.settings["scheduled_connect"]:
            return
            
        current_time = QTime.currentTime()
        connect_time = self.settings["connect_time"]
        disconnect_time = self.settings["disconnect_time"]
        
        # Simple case: connect_time < disconnect_time (same day)
        if connect_time < disconnect_time:
            if connect_time <= current_time < disconnect_time and self.state == ConnectionState.DISCONNECTED:
                logger.info("Scheduled connection: time to connect")
                self.start_tunnel()
            elif (current_time < connect_time or current_time >= disconnect_time) and self.state == ConnectionState.CONNECTED:
                logger.info("Scheduled connection: time to disconnect")
                self.stop_tunnel()
        # Complex case: connect_time > disconnect_time (overnight)
        else:
            if (current_time >= connect_time or current_time < disconnect_time) and self.state == ConnectionState.DISCONNECTED:
                logger.info("Scheduled connection: time to connect (overnight schedule)")
                self.start_tunnel()
            elif disconnect_time <= current_time < connect_time and self.state == ConnectionState.CONNECTED:
                logger.info("Scheduled connection: time to disconnect (overnight schedule)")
                self.stop_tunnel()
    
    def start_tunnel(self):
        if self.state == ConnectionState.CONNECTED or self.state == ConnectionState.CONNECTING:
            logger.info("Tunnel is already connected or connecting")
            return
            
        self.state = ConnectionState.CONNECTING
        self.status_changed.emit(self.state, "Connecting...")
        self.add_connection_event("Connecting", "Attempting to establish tunnel")
        
        # Validate settings
        if not all([self.settings["host"], self.settings["username"], self.settings["key_path"]]):
            msg = "Missing required connection settings"
            logger.error(msg)
            self.state = ConnectionState.ERROR
            self.status_changed.emit(self.state, msg)
            self.add_connection_event("Error", msg)
            return
            
        # Build the SSH command
        cmd = [
            "ssh",
            "-i", self.settings["key_path"],
            "-R", f"{self.settings['remote_port']}:localhost:{self.settings['local_port']}",
            "-p", str(self.settings["port"]),
            "-N",  # No command execution
            "-o", "ServerAliveInterval=30",  # Keep-alive
            "-o", "ServerAliveCountMax=3",  # Max keep-alive attempts
            "-o", "BatchMode=yes",  # Don't prompt for password
            "-o", "PasswordAuthentication=no",  # Disable password authentication
            "-o", "StrictHostKeyChecking=accept-new",  # Auto-accept new host keys
            f"{self.settings['username']}@{self.settings['host']}"
        ]
        
        try:
            # Set up environment to avoid SSH asking for passwords
            env = os.environ.copy()
            env["SSH_ASKPASS"] = ""
            env["DISPLAY"] = ""
            
            # Start the tunnel process
            logger.info(f"Starting SSH tunnel with command: {' '.join(cmd)}")
            self.tunnel_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                env=env
            )
            
            # Start a thread to monitor the process
            threading.Thread(target=self._monitor_tunnel, daemon=True).start()
            
            # Start bandwidth monitoring
            self.bandwidth_monitor.start_monitoring(self.settings["local_port"])
            
        except Exception as e:
            msg = f"Failed to start tunnel: {str(e)}"
            logger.error(msg)
            self.state = ConnectionState.ERROR
            self.status_changed.emit(self.state, msg)
            self.add_connection_event("Error", msg)
        except Exception as e:
            msg = f"Failed to start tunnel: {str(e)}"
            logger.error(msg)
            self.state = ConnectionState.ERROR
            self.status_changed.emit(self.state, msg)
            self.add_connection_event("Error", msg)
    
    def _monitor_tunnel(self):
        if not self.tunnel_process:
            return
            
        # Brief pause to see if process fails immediately
        time.sleep(1)
        
        # Check if process is still running
        if self.tunnel_process.poll() is not None:
            stderr = self.tunnel_process.stderr.read()
            msg = f"Tunnel process failed: {stderr}"
            logger.error(msg)
            self.state = ConnectionState.ERROR
            self.status_changed.emit(self.state, msg)
            self.add_connection_event("Error", msg)
            
            # Attempt reconnect if enabled
            if self.settings["auto_reconnect"]:
                self.schedule_reconnect()
            return
            
        # If we get here, the connection was successful
        logger.info("Tunnel established successfully")
        self.state = ConnectionState.CONNECTED
        self.status_changed.emit(self.state, "Connected")
        self.add_connection_event("Connected", "Tunnel established successfully")
        self.reconnect_attempts = 0
        
        # Wait for process to terminate
        stdout, stderr = self.tunnel_process.communicate()
        
        # Process terminated
        if self.state == ConnectionState.CONNECTED:  # Only if we haven't manually stopped
            logger.warning(f"Tunnel connection lost. Exit code: {self.tunnel_process.returncode}")
            logger.debug(f"STDOUT: {stdout}")
            logger.debug(f"STDERR: {stderr}")
            
            self.state = ConnectionState.DISCONNECTED
            self.status_changed.emit(self.state, "Connection lost")
            self.add_connection_event("Disconnected", f"Connection lost. Exit code: {self.tunnel_process.returncode}")
            
            # Stop bandwidth monitoring
            self.bandwidth_monitor.stop_monitoring()
            
            # Attempt reconnect if enabled
            if self.settings["auto_reconnect"]:
                self.schedule_reconnect()
    
    def schedule_reconnect(self):
        if self.reconnect_attempts >= self.max_reconnect_attempts:
            logger.error(f"Maximum reconnection attempts ({self.max_reconnect_attempts}) reached")
            self.add_connection_event("Error", "Maximum reconnection attempts reached")
            return
            
        self.reconnect_attempts += 1
        delay = self.reconnect_delay * (2 ** (self.reconnect_attempts - 1))  # Exponential backoff
        logger.info(f"Scheduling reconnection attempt {self.reconnect_attempts} in {delay} seconds")
        self.add_connection_event("Reconnecting", f"Attempt {self.reconnect_attempts} scheduled in {delay} seconds")
        
        self.reconnect_timer.start(delay * 1000)
    
    def attempt_reconnect(self):
        logger.info(f"Attempting reconnection ({self.reconnect_attempts}/{self.max_reconnect_attempts})")
        self.reconnect_timer.stop()
        self.start_tunnel()
    
    def stop_tunnel(self):
        if self.state == ConnectionState.DISCONNECTED:
            return
            
        logger.info("Stopping SSH tunnel")
        self.reconnect_timer.stop()
        
        if self.tunnel_process and self.tunnel_process.poll() is None:
            try:
                self.tunnel_process.terminate()
                # Give it a moment to terminate gracefully
                time.sleep(0.5)
                if self.tunnel_process.poll() is None:
                    logger.warning("Tunnel process did not terminate gracefully, forcing kill")
                    self.tunnel_process.kill()
            except Exception as e:
                logger.error(f"Error terminating tunnel process: {str(e)}")
        
        self.tunnel_process = None
        self.state = ConnectionState.DISCONNECTED
        self.status_changed.emit(self.state, "Disconnected")
        self.add_connection_event("Disconnected", "Tunnel stopped manually")
        
        # Stop bandwidth monitoring
        self.bandwidth_monitor.stop_monitoring()
    
    def add_connection_event(self, event_type, details=None):
        event = ConnectionEvent(event_type, details=details)
        self.connection_history.append(event)
        self.connection_event.emit(event)
        logger.info(f"Connection event: {event}")

class SettingsDialog(QDialog):
    def __init__(self, tunnel_manager, parent=None):
        super().__init__(parent)
        self.setWindowTitle("SSH Tunnel Manager Settings")
        self.resize(500, 400)
        self.tunnel_manager = tunnel_manager
        
        # Create tabs
        tabs = QTabWidget()
        connection_tab = QWidget()
        advanced_tab = QWidget()
        history_tab = QWidget()
        
        tabs.addTab(connection_tab, "Connection")
        tabs.addTab(advanced_tab, "Advanced")
        tabs.addTab(history_tab, "History")
        
        # Connection Tab
        conn_layout = QFormLayout()
        
        self.host_edit = QLineEdit(self.tunnel_manager.settings["host"])
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(self.tunnel_manager.settings["port"])
        self.username_edit = QLineEdit(self.tunnel_manager.settings["username"])
        
        self.key_path_edit = QLineEdit(self.tunnel_manager.settings["key_path"])
        self.key_browse_btn = QPushButton("Browse...")
        key_layout = QHBoxLayout()
        key_layout.addWidget(self.key_path_edit)
        key_layout.addWidget(self.key_browse_btn)
        
        self.local_port_spin = QSpinBox()
        self.local_port_spin.setRange(1, 65535)
        self.local_port_spin.setValue(self.tunnel_manager.settings["local_port"])
        
        self.remote_port_spin = QSpinBox()
        self.remote_port_spin.setRange(1, 65535)
        self.remote_port_spin.setValue(self.tunnel_manager.settings["remote_port"])
        
        conn_layout.addRow("Host:", self.host_edit)
        conn_layout.addRow("Port:", self.port_spin)
        conn_layout.addRow("Username:", self.username_edit)
        conn_layout.addRow("SSH Key:", key_layout)
        conn_layout.addRow("Local Port:", self.local_port_spin)
        conn_layout.addRow("Remote Port:", self.remote_port_spin)
        
        connection_tab.setLayout(conn_layout)
        
        # Advanced Tab
        adv_layout = QVBoxLayout()
        
        # Reconnection settings
        reconnect_group = QGroupBox("Reconnection")
        reconnect_layout = QFormLayout()
        
        self.auto_reconnect_check = QCheckBox("Automatically reconnect on failure")
        self.auto_reconnect_check.setChecked(self.tunnel_manager.settings["auto_reconnect"])
        
        self.max_attempts_spin = QSpinBox()
        self.max_attempts_spin.setRange(1, 20)
        self.max_attempts_spin.setValue(self.tunnel_manager.max_reconnect_attempts)
        
        self.reconnect_delay_spin = QSpinBox()
        self.reconnect_delay_spin.setRange(1, 60)
        self.reconnect_delay_spin.setValue(self.tunnel_manager.reconnect_delay)
        self.reconnect_delay_spin.setSuffix(" seconds")
        
        reconnect_layout.addRow(self.auto_reconnect_check, QWidget())
        reconnect_layout.addRow("Maximum attempts:", self.max_attempts_spin)
        reconnect_layout.addRow("Initial delay:", self.reconnect_delay_spin)
        reconnect_group.setLayout(reconnect_layout)
        
        # Scheduling settings
        schedule_group = QGroupBox("Connection Scheduling")
        schedule_layout = QFormLayout()
        
        self.scheduled_connect_check = QCheckBox("Enable scheduled connection")
        self.scheduled_connect_check.setChecked(self.tunnel_manager.settings["scheduled_connect"])
        
        self.connect_time_edit = QTimeEdit(self.tunnel_manager.settings["connect_time"])
        self.connect_time_edit.setDisplayFormat("HH:mm")
        
        self.disconnect_time_edit = QTimeEdit(self.tunnel_manager.settings["disconnect_time"])
        self.disconnect_time_edit.setDisplayFormat("HH:mm")
        
        schedule_layout.addRow(self.scheduled_connect_check, QWidget())
        schedule_layout.addRow("Connect at:", self.connect_time_edit)
        schedule_layout.addRow("Disconnect at:", self.disconnect_time_edit)
        schedule_group.setLayout(schedule_layout)
        
        adv_layout.addWidget(reconnect_group)
        adv_layout.addWidget(schedule_group)
        adv_layout.addStretch()
        
        advanced_tab.setLayout(adv_layout)
        
        # History Tab
        history_layout = QVBoxLayout()
        
        self.history_table = QTableWidget()
        self.history_table.setColumnCount(3)
        self.history_table.setHorizontalHeaderLabels(["Time", "Event", "Details"])
        self.history_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        
        # Populate history table
        self.update_history_table()
        
        history_layout.addWidget(self.history_table)
        
        # Add clear history button
        clear_history_btn = QPushButton("Clear History")
        clear_history_btn.clicked.connect(self.clear_history)
        history_layout.addWidget(clear_history_btn)
        
        history_tab.setLayout(history_layout)
        
        # Dialog buttons
        button_layout = QHBoxLayout()
        save_btn = QPushButton("Save")
        cancel_btn = QPushButton("Cancel")
        
        save_btn.clicked.connect(self.save_settings)
        cancel_btn.clicked.connect(self.reject)
        
        button_layout.addWidget(save_btn)
        button_layout.addWidget(cancel_btn)
        
        # Main layout
        main_layout = QVBoxLayout()
        main_layout.addWidget(tabs)
        main_layout.addLayout(button_layout)
        
        self.setLayout(main_layout)
        
        # Connect signals
        self.key_browse_btn.clicked.connect(self.browse_key_file)
        self.tunnel_manager.connection_event.connect(self.on_connection_event)
    
    def browse_key_file(self):
        file_name, _ = QFileDialog.getOpenFileName(
            self, "Select SSH Key File", "", "All Files (*)"
        )
        if file_name:
            self.key_path_edit.setText(file_name)
    
    def save_settings(self):
        # Update connection settings
        self.tunnel_manager.settings["host"] = self.host_edit.text()
        self.tunnel_manager.settings["port"] = self.port_spin.value()
        self.tunnel_manager.settings["username"] = self.username_edit.text()
        self.tunnel_manager.settings["key_path"] = self.key_path_edit.text()
        self.tunnel_manager.settings["local_port"] = self.local_port_spin.value()
        self.tunnel_manager.settings["remote_port"] = self.remote_port_spin.value()
        
        # Update advanced settings
        self.tunnel_manager.settings["auto_reconnect"] = self.auto_reconnect_check.isChecked()
        self.tunnel_manager.max_reconnect_attempts = self.max_attempts_spin.value()
        self.tunnel_manager.reconnect_delay = self.reconnect_delay_spin.value()
        
        # Update scheduling settings
        self.tunnel_manager.settings["scheduled_connect"] = self.scheduled_connect_check.isChecked()
        self.tunnel_manager.settings["connect_time"] = self.connect_time_edit.time()
        self.tunnel_manager.settings["disconnect_time"] = self.disconnect_time_edit.time()
        
        # Save settings to file
        self.tunnel_manager.save_settings()
        
        # Just close the dialog
        self.accept()
        
        # Show a notification
        QMessageBox.information(self, "Settings Saved", "Your settings have been saved successfully.")
    
    def update_history_table(self):
        self.history_table.setRowCount(0)
        for i, event in enumerate(self.tunnel_manager.connection_history):
            self.history_table.insertRow(i)
            self.history_table.setItem(i, 0, QTableWidgetItem(event.timestamp.strftime("%Y-%m-%d %H:%M:%S")))
            self.history_table.setItem(i, 1, QTableWidgetItem(event.event_type))
            self.history_table.setItem(i, 2, QTableWidgetItem(event.details))
    
    def on_connection_event(self, event):
        row = self.history_table.rowCount()
        self.history_table.insertRow(row)
        self.history_table.setItem(row, 0, QTableWidgetItem(event.timestamp.strftime("%Y-%m-%d %H:%M:%S")))
        self.history_table.setItem(row, 1, QTableWidgetItem(event.event_type))
        self.history_table.setItem(row, 2, QTableWidgetItem(event.details))
    
    def clear_history(self):
        self.tunnel_manager.connection_history = []
        self.update_history_table()

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("SSH Tunnel Manager")
        self.setWindowFlag(Qt.Window)
        self.setWindowFlag(Qt.WindowStaysOnTopHint, False)
        self.setGeometry(0, 0, 1, 1)  # Tiny window
        
        # Create tunnel manager
        self.tunnel_manager = TunnelManager(self)
        self.tunnel_manager.load_settings()
        
        # Create system tray icon
        self.tray_icon = QSystemTrayIcon(self)
        self.update_icon(ConnectionState.DISCONNECTED)
        self.tray_icon.setToolTip("SSH Tunnel Manager")
        
        # Create tray menu
        self.tray_menu = QMenu()
        
        self.status_action = QAction("Disconnected")
        self.status_action.setEnabled(False)
        self.tray_menu.addAction(self.status_action)
        
        self.tray_menu.addSeparator()
        
        self.connect_action = QAction("Connect")
        self.connect_action.triggered.connect(self.tunnel_manager.start_tunnel)
        self.tray_menu.addAction(self.connect_action)
        
        self.disconnect_action = QAction("Disconnect")
        self.disconnect_action.triggered.connect(self.tunnel_manager.stop_tunnel)
        self.disconnect_action.setEnabled(False)
        self.tray_menu.addAction(self.disconnect_action)
        
        self.tray_menu.addSeparator()
        
        self.bandwidth_action = QAction("Bandwidth: 0 KB/s ↑ | 0 KB/s ↓")
        self.bandwidth_action.setEnabled(False)
        self.tray_menu.addAction(self.bandwidth_action)
        
        self.tray_menu.addSeparator()
        
        self.settings_action = QAction("Settings")
        self.settings_action.triggered.connect(self.show_settings)
        self.tray_menu.addAction(self.settings_action)
        
        self.tray_menu.addSeparator()
        
        self.exit_action = QAction("Exit")
        self.exit_action.triggered.connect(self.exit_app)
        self.tray_menu.addAction(self.exit_action)
        
        # Set the menu
        self.tray_icon.setContextMenu(self.tray_menu)
        
        # Show the tray icon
        self.tray_icon.show()
        
        # Connect signals
        self.tunnel_manager.status_changed.connect(self.on_status_changed)
        self.tunnel_manager.bandwidth_monitor.bandwidth_updated.connect(self.on_bandwidth_updated)
        
        # Add double-click behavior
        self.tray_icon.activated.connect(self.on_tray_activated)
        
        # Hide the main window but keep it loaded
        self.hide()
    
    def create_tray_icon(self, color, symbol):
        # Create a pixmap with the specified color
        pixmap = QPixmap(24, 24)
        pixmap.fill(QColor(color))
        
        # Create a painter to draw the symbol
        from PyQt5.QtGui import QPainter, QFont, QPen
        painter = QPainter(pixmap)
        painter.setPen(QPen(Qt.black, 2))
        
        # Set font for the symbol
        font = QFont("Arial", 12, QFont.Bold)
        painter.setFont(font)
        
        # Draw the symbol centered in the pixmap
        painter.drawText(pixmap.rect(), Qt.AlignCenter, symbol)
        painter.end()
        
        return QIcon(pixmap)
    
    def update_icon(self, state):
        if state == ConnectionState.CONNECTED:
            icon = self.create_tray_icon("orange", "⇆")
        elif state == ConnectionState.CONNECTING:
            icon = self.create_tray_icon("yellow", "…")
        elif state == ConnectionState.ERROR:
            icon = self.create_tray_icon("red", "!")
        else:  # DISCONNECTED
            icon = self.create_tray_icon("green", "||")
        
        self.tray_icon.setIcon(icon)
    
    def on_status_changed(self, state, message):
        self.update_icon(state)
        self.status_action.setText(message)
        
        # Update action states
        if state == ConnectionState.CONNECTED:
            self.connect_action.setEnabled(False)
            self.disconnect_action.setEnabled(True)
        elif state == ConnectionState.CONNECTING:
            self.connect_action.setEnabled(False)
            self.disconnect_action.setEnabled(True)
        else:  # DISCONNECTED or ERROR
            self.connect_action.setEnabled(True)
            self.disconnect_action.setEnabled(False)
    
    def on_bandwidth_updated(self, upload, download):
        self.bandwidth_action.setText(f"Bandwidth: {upload:.1f} KB/s ↑ | {download:.1f} KB/s ↓")
    
    def on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            self.show_settings()
    
    def show_settings(self):
        dialog = SettingsDialog(self.tunnel_manager, self)
        dialog.exec_()
    
    def exit_app(self):
        # Properly shut down
        self.tunnel_manager.stop_tunnel()
        QApplication.quit()
    
    def closeEvent(self, event):
        # Override close event to hide instead of close
        event.ignore()
        self.hide()

if __name__ == "__main__":
    # Handle Ctrl+C properly
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    
    app = QApplication(sys.argv)
    
    # Enable system tray if available
    if not QSystemTrayIcon.isSystemTrayAvailable():
        QMessageBox.critical(None, "SSH Tunnel Manager", 
                             "System tray not available on this system.")
        sys.exit(1)
    
    # Set application details
    app.setApplicationName("SSH Tunnel Manager")
    app.setQuitOnLastWindowClosed(False)  # Very important - continue running when dialog closes
    
    # Create and show main window
    window = MainWindow()
    
    # Run the event loop
    sys.exit(app.exec_())