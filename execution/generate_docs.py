import os
import subprocess
import sys

def main():
    """
    Automated documentation generation script for ARES.
    Uses 'pdoc' to generate HTML API documentation from Python source files.
    """
    print("[+] Generating API documentation using pdoc...")
    
    # Ensure pdoc is installed
    try:
        import pdoc
    except ImportError:
        print("[-] 'pdoc' is not installed. Installing it now...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pdoc"])
        
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    docs_dir = os.path.join(project_root, "directives", "api-docs", "html")
    
    # We will generate docs for the main modules
    modules = ["alerts", "config", "engine", "main", "models", "storage", "detectors", "fetchers"]
    
    cmd = [
        sys.executable, "-m", "pdoc",
        "--output-dir", docs_dir,
        "--docformat", "google"
    ] + modules
    
    try:
        subprocess.check_call(cmd, cwd=project_root)
        print(f"[+] Documentation generated successfully at: {docs_dir}")
    except subprocess.CalledProcessError as e:
        print(f"[-] Failed to generate documentation: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
