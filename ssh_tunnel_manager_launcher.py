#!/usr/bin/env python3
import subprocess
import sys
import os
import time

# Get the directory of this script
script_dir = os.path.dirname(os.path.abspath(__file__))

def main():
    print("Starting SSH Tunnel Manager...")
    
    # Path to the main script
    main_script = os.path.join(script_dir, "ssh_tunnel_manager_app.py")
    
    # Make sure the script exists
    if not os.path.exists(main_script):
        print(f"Error: Could not find {main_script}")
        sys.exit(1)
    
    # Make it executable
    os.chmod(main_script, 0o755)
    
    # Run the application in a separate process
    try:
        process = subprocess.Popen([sys.executable, main_script])
        
        # Wait for the process to complete
        process.wait()
        
        print("SSH Tunnel Manager has exited.")
    except KeyboardInterrupt:
        print("\nExiting SSH Tunnel Manager...")
        sys.exit(0)

if __name__ == "__main__":
    main()