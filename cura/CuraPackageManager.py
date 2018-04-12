# Copyright (c) 2018 Ultimaker B.V.
# Cura is released under the terms of the LGPLv3 or higher.

from typing import Optional
import json
import os
import shutil
import zipfile
import tempfile

from PyQt5.QtCore import pyqtSlot, QObject, pyqtSignal

from UM.Logger import Logger
from UM.Resources import Resources

from cura.Utils import VersionTools


class CuraPackageManager(QObject):

    # The prefix that's added to all files for an installed package to avoid naming conflicts with user created
    # files.
    PREFIX_PLACE_HOLDER = "-CP;"

    def __init__(self, parent = None):
        super().__init__(parent)

        self._application = parent
        self._container_registry = self._application.getContainerRegistry()
        self._plugin_registry = self._application.getPluginRegistry()

        # JSON file that keeps track of all installed packages.
        self._package_management_file_path = os.path.join(os.path.abspath(Resources.getDataStoragePath()),
                                                          "packages.json")
        self._installed_package_dict = {}  # a dict of all installed packages
        self._to_remove_package_set = set()  # a set of packages that need to be removed at the next start
        self._to_install_package_dict = {}  # a dict of packages that need to be installed at the next start

    installedPackagesChanged = pyqtSignal()  # Emitted whenever the installed packages collection have been changed.

    def initialize(self):
        self._loadManagementData()
        self._removeAllScheduledPackages()
        self._installAllScheduledPackages()

    # (for initialize) Loads the package management file if exists
    def _loadManagementData(self) -> None:
        if not os.path.exists(self._package_management_file_path):
            Logger.log("i", "Package management file %s doesn't exist, do nothing", self._package_management_file_path)
            return

        with open(self._package_management_file_path, "r", encoding = "utf-8") as f:
            management_dict = json.loads(f.read(), encoding = "utf-8")

            self._installed_package_dict = management_dict["installed"]
            self._to_remove_package_set = set(management_dict["to_remove"])
            self._to_install_package_dict = management_dict["to_install"]

            Logger.log("i", "Package management file %s is loaded", self._package_management_file_path)

    def _saveManagementData(self) -> None:
        with open(self._package_management_file_path, "w", encoding = "utf-8") as f:
            data_dict = {"installed": self._installed_package_dict,
                         "to_remove": list(self._to_remove_package_set),
                         "to_install": self._to_install_package_dict}
            data_dict["to_remove"] = list(data_dict["to_remove"])
            json.dump(data_dict, f)
            Logger.log("i", "Package management file %s is saved", self._package_management_file_path)

    # (for initialize) Removes all packages that have been scheduled to be removed.
    def _removeAllScheduledPackages(self) -> None:
        for package_id in self._to_remove_package_set:
            self._purgePackage(package_id)
        self._to_remove_package_set.clear()
        self._saveManagementData()

    # (for initialize) Installs all packages that have been scheduled to be installed.
    def _installAllScheduledPackages(self) -> None:
        for package_id, installation_package_data in self._to_install_package_dict.items():
            self._installPackage(installation_package_data)
        self._to_install_package_dict.clear()
        self._saveManagementData()

    # Checks the given package is installed. If so, return a dictionary that contains the package's information.
    def getInstalledPackageInfo(self, package_id: str) -> Optional[dict]:
        if package_id in self._to_remove_package_set:
            return None
        if package_id in self._to_install_package_dict:
            return self._to_install_package_dict[package_id]["package_info"]

        return self._installed_package_dict.get(package_id)

    # Checks if the given package is installed.
    def isPackageInstalled(self, package_id: str) -> bool:
        return self.getInstalledPackageInfo(package_id) is not None

    # Schedules the given package file to be installed upon the next start.
    @pyqtSlot(str)
    def installPackage(self, filename: str) -> None:
        # Get package information
        package_info = self.getPackageInfo(filename)
        package_id = package_info["package_id"]

        has_changes = False
        # Check the delayed installation and removal lists first
        if package_id in self._to_remove_package_set:
            self._to_remove_package_set.remove(package_id)
            has_changes = True

        # Check if it is installed
        installed_package_info = self.getInstalledPackageInfo(package_info["package_id"])
        to_install_package = installed_package_info is None  # Install if the package has not been installed
        if installed_package_info is not None:
            # Compare versions and only schedule the installation if the given package is newer
            new_version = package_info["package_version"]
            installed_version = installed_package_info["package_version"]
            if VersionTools.compareSemanticVersions(new_version, installed_version) > 0:
                Logger.log("i", "Package [%s] version [%s] is newer than the installed version [%s], update it.",
                           package_id, new_version, installed_version)
                to_install_package = True

        if to_install_package:
            Logger.log("i", "Package [%s] version [%s] is scheduled to be installed.",
                       package_id, package_info["package_version"])
            # Copy the file to cache dir so we don't need to rely on the original file to be present
            package_cache_dir = os.path.join(os.path.abspath(Resources.getCacheStoragePath()), "cura_packages")
            if not os.path.exists(package_cache_dir):
                os.makedirs(package_cache_dir, exist_ok=True)

            target_file_path = os.path.join(package_cache_dir, package_id + ".curapackage")
            shutil.copy2(filename, target_file_path)

            self._to_install_package_dict[package_id] = {"package_info": package_info,
                                                         "filename": target_file_path}
            has_changes = True

        self._saveManagementData()
        if has_changes:
            self.installedPackagesChanged.emit()

    # Schedules the given package to be removed upon the next start.
    @pyqtSlot(str)
    def removePackage(self, package_id: str) -> None:
        # Check the delayed installation and removal lists first
        if not self.isPackageInstalled(package_id):
            Logger.log("i", "Attempt to remove package [%s] that is not installed, do nothing.", package_id)
            return

        # Remove from the delayed installation list if present
        if package_id in self._to_install_package_dict:
            del self._to_install_package_dict[package_id]

        # If the package has already been installed, schedule for a delayed removal
        if package_id in self._installed_package_dict:
            self._to_remove_package_set.add(package_id)

        self._saveManagementData()
        self.installedPackagesChanged.emit()

    # Removes everything associated with the given package ID.
    def _purgePackage(self, package_id: str) -> None:
        # Get all folders that need to be checked for installed packages, including:
        #  - materials
        #  - qualities
        #  - plugins
        from cura.CuraApplication import CuraApplication
        dirs_to_check = [
            Resources.getStoragePath(CuraApplication.ResourceTypes.MaterialInstanceContainer),
            Resources.getStoragePath(CuraApplication.ResourceTypes.QualityInstanceContainer),
            os.path.join(os.path.abspath(Resources.getDataStoragePath()), "plugins"),
        ]

        for root_dir in dirs_to_check:
            package_dir = os.path.join(root_dir, package_id)
            if os.path.exists(package_dir):
                Logger.log("i", "Removing '%s' for package [%s]", package_dir, package_id)
                shutil.rmtree(package_dir)

    # Installs all files associated with the given package.
    def _installPackage(self, installation_package_data: dict):
        package_info = installation_package_data["package_info"]
        filename = installation_package_data["filename"]

        package_id = package_info["package_id"]

        if not os.path.exists(filename):
            Logger.log("w", "Package [%s] file '%s' is missing, cannot install this package", package_id, filename)
            return

        Logger.log("i", "Installing package [%s] from file [%s]", package_id, filename)

        # If it's installed, remove it first and then install
        if package_id in self._installed_package_dict:
            self._purgePackage(package_id)

        # Install the package
        archive = zipfile.ZipFile(filename, "r")

        temp_dir = tempfile.TemporaryDirectory()
        archive.extractall(temp_dir.name)

        from cura.CuraApplication import CuraApplication
        installation_dirs_dict = {
            "materials": Resources.getStoragePath(CuraApplication.ResourceTypes.MaterialInstanceContainer),
            "quality": Resources.getStoragePath(CuraApplication.ResourceTypes.QualityInstanceContainer),
            "plugins": os.path.join(os.path.abspath(Resources.getDataStoragePath()), "plugins"),
        }

        for sub_dir_name, installation_root_dir in installation_dirs_dict.items():
            src_dir_path = os.path.join(temp_dir.name, "files", sub_dir_name)
            dst_dir_path = os.path.join(installation_root_dir, package_id)

            if not os.path.exists(src_dir_path):
                continue

            # Need to rename the container files so they don't get ID conflicts
            to_rename_files = sub_dir_name not in ("plugins",)
            self.__installPackageFiles(package_id, src_dir_path, dst_dir_path, need_to_rename_files= to_rename_files)

        archive.close()

        # Remove the file
        os.remove(filename)

    def __installPackageFiles(self, package_id: str, src_dir: str, dst_dir: str, need_to_rename_files: bool = True) -> None:
        shutil.move(src_dir, dst_dir)

        # Rename files if needed
        if not need_to_rename_files:
            return
        for root, _, file_names in os.walk(dst_dir):
            for filename in file_names:
                new_filename = self.PREFIX_PLACE_HOLDER + package_id + "-" + filename
                old_file_path = os.path.join(root, filename)
                new_file_path = os.path.join(root, new_filename)
                os.rename(old_file_path, new_file_path)

    # Gets package information from the given file.
    def getPackageInfo(self, filename: str) -> dict:
        archive = zipfile.ZipFile(filename, "r")
        try:
            # All information is in package.json
            with archive.open("package.json", "r") as f:
                package_info_dict = json.loads(f.read().decode("utf-8"))
                return package_info_dict
        except Exception as e:
            raise RuntimeError("Could not get package information from file '%s': %s" % (filename, e))
        finally:
            archive.close()

    # Gets the license file content if present in the given package file.
    # Returns None if there is no license file found.
    def getPackageLicense(self, filename: str) -> Optional[str]:
        license_string = None
        archive = zipfile.ZipFile(filename)
        try:
            # Go through all the files and use the first successful read as the result
            for file_info in archive.infolist():
                if file_info.is_dir() or not file_info.filename.startswith("files/"):
                    continue

                filename_parts = os.path.basename(file_info.filename.lower()).split(".")
                stripped_filename = filename_parts[0]
                if stripped_filename in ("license", "licence"):
                    Logger.log("i", "Found potential license file '%s'", file_info.filename)
                    try:
                        with archive.open(file_info.filename, "r") as f:
                            data = f.read()
                        license_string = data.decode("utf-8")
                        break
                    except:
                        Logger.logException("e", "Failed to load potential license file '%s' as text file.",
                                            file_info.filename)
                        license_string = None
        except Exception as e:
            raise RuntimeError("Could not get package license from file '%s': %s" % (filename, e))
        finally:
            archive.close()
        return license_string
