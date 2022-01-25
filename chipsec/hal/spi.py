# CHIPSEC: Platform Security Assessment Framework
# Copyright (c) 2010-2021, Intel Corporation

# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; Version 2.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.

# Contact information:
# chipsec@intel.com

"""
Access to SPI Flash parts

usage:
    >>> read_spi(spi_fla, length)
    >>> write_spi(spi_fla, buf)
    >>> erase_spi_block(spi_fla)
    >>> get_SPI_JEDEC_ID()
    >>> get_SPI_JEDEC_ID_decoded()

.. note::
    !! IMPORTANT:
    Size of the data chunk used in SPI read cycle (in bytes)
    default = maximum 64 bytes (remainder is read in 4 byte chunks)

    If you want to change logic to read SPI Flash in 4 byte chunks:
    SPI_READ_WRITE_MAX_DBC = 4

    @TBD: SPI write cycles operate on 4 byte chunks (not optimized yet)

    Approximate performance (on 2-core SMT Intel Core i5-4300U (Haswell) CPU 1.9GHz):
    SPI read: ~7 sec per 1MB (with DBC=64)
"""

from binascii import hexlify
import struct
import time

from chipsec.defines import ALIGNED_4KB, BIT0, BIT1, BIT2, BIT5
from chipsec.file import write_file, read_file
from chipsec.logger import print_buffer, print_buffer_bytes
from chipsec.hal import hal_base
from chipsec.helper import oshelper
from chipsec.lib.spi_jedec_ids import JEDEC_ID
from chipsec.exceptions import SpiRuntimeError

SPI_READ_WRITE_MAX_DBC = 64
SPI_READ_WRITE_DEF_DBC = 4
SFDP_HEADER = 0x50444653

SPI_MAX_PR_COUNT  = 5
SPI_FLA_SHIFT     = 12
SPI_FLA_PAGE_MASK = ALIGNED_4KB

SPI_MMIO_BASE_LENGTH = 0x200
PCH_RCBA_SPI_HSFSTS_SCIP  = BIT5   # SPI cycle in progress
PCH_RCBA_SPI_HSFSTS_AEL   = BIT2   # Access Error Log
PCH_RCBA_SPI_HSFSTS_FCERR = BIT1   # Flash Cycle Error
PCH_RCBA_SPI_HSFSTS_FDONE = BIT0   # Flash Cycle Done

PCH_RCBA_SPI_HSFCTL_FCYCLE_READ  = 0  # Flash Cycle Read
PCH_RCBA_SPI_HSFCTL_FCYCLE_WRITE = 2  # Flash Cycle Write
PCH_RCBA_SPI_HSFCTL_FCYCLE_ERASE = 3  # Flash Cycle Block Erase
PCH_RCBA_SPI_HSFCTL_FCYCLE_SFDP  = 5
PCH_RCBA_SPI_HSFCTL_FCYCLE_JEDEC = 6  # Flash Cycle Read JEDEC ID
PCH_RCBA_SPI_HSFCTL_FCYCLE_FGO   = BIT0  # Flash Cycle GO

PCH_RCBA_SPI_FADDR_MASK = 0x07FFFFFF  # SPI Flash Address Mask [0:26]

PCH_RCBA_SPI_FREGx_LIMIT_MASK = 0x7FFF0000  # Size
PCH_RCBA_SPI_FREGx_BASE_MASK  = 0x00007FFF  # Base

PCH_RCBA_SPI_OPTYPE_RDNOADDR = 0x00
PCH_RCBA_SPI_OPTYPE_WRNOADDR = 0x01
PCH_RCBA_SPI_OPTYPE_RDADDR   = 0x02
PCH_RCBA_SPI_OPTYPE_WRADDR   = 0x03

PCH_RCBA_SPI_FDOC_FDSS_FSDM = 0x0000  # Flash Signature and Descriptor Map
PCH_RCBA_SPI_FDOC_FDSS_COMP = 0x1000  # Component
PCH_RCBA_SPI_FDOC_FDSS_REGN = 0x2000  # Region
PCH_RCBA_SPI_FDOC_FDSS_MSTR = 0x3000  # Master
PCH_RCBA_SPI_FDOC_FDSI_MASK = 0x0FFC  # Flash Descriptor Section Index

# agregated SPI Flash commands
HSFCTL_READ_CYCLE = ((PCH_RCBA_SPI_HSFCTL_FCYCLE_READ << 1) | PCH_RCBA_SPI_HSFCTL_FCYCLE_FGO)
HSFCTL_WRITE_CYCLE = ((PCH_RCBA_SPI_HSFCTL_FCYCLE_WRITE << 1) | PCH_RCBA_SPI_HSFCTL_FCYCLE_FGO)
HSFCTL_ERASE_CYCLE = ((PCH_RCBA_SPI_HSFCTL_FCYCLE_ERASE << 1) | PCH_RCBA_SPI_HSFCTL_FCYCLE_FGO)
HSFCTL_JEDEC_CYCLE = ((PCH_RCBA_SPI_HSFCTL_FCYCLE_JEDEC << 1) | PCH_RCBA_SPI_HSFCTL_FCYCLE_FGO)
HSFCTL_SFDP_CYCLE = ((PCH_RCBA_SPI_HSFCTL_FCYCLE_SFDP << 1) | PCH_RCBA_SPI_HSFCTL_FCYCLE_FGO)

HSFSTS_CLEAR = (PCH_RCBA_SPI_HSFSTS_AEL | PCH_RCBA_SPI_HSFSTS_FCERR | PCH_RCBA_SPI_HSFSTS_FDONE)

#
# Hardware Sequencing Flash Status (HSFSTS)
#
SPI_HSFSTS_OFFSET = 0x04
# HSFSTS bit masks
SPI_HSFSTS_FLOCKDN_MASK = (1 << 15)
SPI_HSFSTS_FDOPSS_MASK = (1 << 13)

#
# Flash Regions
#

SPI_REGION_NUMBER_IN_FD = 12

FLASH_DESCRIPTOR    = 0
BIOS                = 1
ME                  = 2
GBE                 = 3
PLATFORM_DATA       = 4
FREG5               = 5
FREG6               = 6
FREG7               = 7
EMBEDDED_CONTROLLER = 8
FREG9               = 9
FREG10              = 10
FREG11              = 11

SPI_REGION = {
    FLASH_DESCRIPTOR: 'FREG0_FLASHD',
    BIOS: 'FREG1_BIOS',
    ME: 'FREG2_ME',
    GBE: 'FREG3_GBE',
    PLATFORM_DATA: 'FREG4_PD',
    FREG5: 'FREG5',
    FREG6: 'FREG6',
    FREG7: 'FREG7',
    EMBEDDED_CONTROLLER: 'FREG8_EC',
    FREG9: 'FREG9',
    FREG10: 'FREG10',
    FREG11: 'FREG11'
}

SPI_REGION_NAMES = {
    FLASH_DESCRIPTOR: 'Flash Descriptor',
    BIOS: 'BIOS',
    ME: 'Intel ME',
    GBE: 'GBe',
    PLATFORM_DATA: 'Platform Data',
    FREG5: 'Flash Region 5',
    FREG6: 'Flash Region 6',
    FREG7: 'Flash Region 7',
    EMBEDDED_CONTROLLER: 'Embedded Controller',
    FREG9: 'Flash Region 9',
    FREG10: 'Flash Region 10',
    FREG11: 'Flash Region 11'
}

#
# Flash Descriptor Master Defines
#

MASTER_HOST_CPU_BIOS = 0
MASTER_ME = 1
MASTER_GBE = 2
MASTER_EC = 3

SPI_MASTER_NAMES = {
    MASTER_HOST_CPU_BIOS: 'CPU',
    MASTER_ME: 'ME',
    MASTER_GBE: 'GBe',
    MASTER_EC: 'EC'
}

SPI_FLASH_DESCRIPTOR_SIGNATURE = struct.pack('=I', 0x0FF0A55A)
SPI_FLASH_DESCRIPTOR_SIZE = 0x1000


class SPI(hal_base.HALBase):

    def __init__(self, cs):
        super(SPI, self).__init__(cs)
        self.cs.set_scope({
            "HSFS": "8086.SPI",
            "HSFC": "8086.SPI",
            "FADDR": "8086.SPI",
            "FDATA0": "8086.SPI",
            "FDATA1": "8086.SPI",
            "FDATA2": "8086.SPI",
            "FDATA3": "8086.SPI",
            "FDATA4": "8086.SPI",
            "FDATA5": "8086.SPI",
            "FDATA6": "8086.SPI",
            "FDATA7": "8086.SPI",
            "FDATA8": "8086.SPI",
            "FDATA9": "8086.SPI",
            "FDATA10": "8086.SPI",
            "FDATA11": "8086.SPI",
            "FDATA12": "8086.SPI",
            "FDATA13": "8086.SPI",
            "FDATA14": "8086.SPI",
            "FDATA15": "8086.SPI",
            "BIOS_PTINX": "8086.SPI",
            "BIOS_PTDATA": "8086.SPI",
            "FDOC": "8086.SPI",
            "FDOD": "8086.SPI",
            "PREOP": "8086.SPI",
            "OPTYPE": "8086.SPI",
            "OPMENU_LO": "8086.SPI",
            "OPMENU_HI": "8086.SPI",
            "BFPR": "8086.SPI",
            "FRAP": "8086.SPI",
            "BC": "8086.SPI",
            "FLMAP0": "8086.SPI",
            "FLMAP1": "8086.SPI",
            "FLMAP2": "8086.SPI",
            "FLREG0": "8086.SPI",
            "FLREG1": "8086.SPI",
            "FLREG2": "8086.SPI",
            "FLREG3": "8086.SPI",
            "FLREG4": "8086.SPI",
            "FLREG5": "8086.SPI",
            "FLREG6": "8086.SPI",
            "FLREG7": "8086.SPI",
            "FLREG8": "8086.SPI",
            "FLREG9": "8086.SPI",
            "FLREG10": "8086.SPI",
            "FLREG11": "8086.SPI",
            "FLMSTR1": "8086.SPI",
            "SPIBAR": "8086.SPI",
            "FREG0_FLASHD": "8086.SPI",
            "FREG1_BIOS": "8086.SPI",
            "FREG2_ME": "8086.SPI",
            "FREG3_GBE": "8086.SPI",
            "FREG4_PD": "8086.SPI",
            "FREG5": "8086.SPI",
            "FREG6": "8086.SPI",
            "FREG7": "8086.SPI",
            "FREG8_EC": "8086.SPI",
            "FREG9": "8086.SPI",
            "FREG10": "8086.SPI",
            "FREG11": "8086.SPI",
            "PR0": "8086.SPI",
            "PR1": "8086.SPI",
            "PR2": "8086.SPI",
            "PR3": "8086.SPI",
            "PR4": "8086.SPI",
            "DWORD0": "8086.SPI",
            "DWORD1": "8086.SPI",
            "DWORD2": "8086.SPI",
            "DWORD3": "8086.SPI",
            "DWORD4": "8086.SPI",
            "DWORD5": "8086.SPI",
            "DWORD6": "8086.SPI",
            "DWORD7": "8086.SPI",
            "DWORD8": "8086.SPI",
            "DWORD9": "8086.SPI",
            "DWORD10": "8086.SPI",
            "DWORD11": "8086.SPI",
            "DWORD12": "8086.SPI",
            "DWORD13": "8086.SPI",
            "DWORD14": "8086.SPI",
            "DWORD15": "8086.SPI",
            "DWORD16": "8086.SPI",
            "DWORD17": "8086.SPI",
            "DWORD18": "8086.SPI",
            "DWORD19": "8086.SPI",
            "DWORD20": "8086.SPI",
        })
        self.rcba_spi_base = self.get_SPI_MMIO_base()
        # We try to map SPIBAR in the process memory, this will increase the
        # speed of MMIO access later on.
        try:
            self.cs.helper.map_io_space(self.rcba_spi_base, SPI_MMIO_BASE_LENGTH, 0)
        except oshelper.UnimplementedAPIError:
            pass

        # Reading definitions of SPI flash controller registers
        # which are required to send SPI cycles once for performance reasons
        self.hsfs_off = int(self.cs.get_register_def("HSFS")['offset'], 16)
        self.hsfc_off = int(self.cs.get_register_def("HSFC")['offset'], 16)
        self.faddr_off = int(self.cs.get_register_def("FADDR")['offset'], 16)
        self.fdata0_off = int(self.cs.get_register_def("FDATA0")['offset'], 16)
        self.fdata1_off = int(self.cs.get_register_def("FDATA1")['offset'], 16)
        self.fdata2_off = int(self.cs.get_register_def("FDATA2")['offset'], 16)
        self.fdata3_off = int(self.cs.get_register_def("FDATA3")['offset'], 16)
        self.fdata4_off = int(self.cs.get_register_def("FDATA4")['offset'], 16)
        self.fdata5_off = int(self.cs.get_register_def("FDATA5")['offset'], 16)
        self.fdata6_off = int(self.cs.get_register_def("FDATA6")['offset'], 16)
        self.fdata7_off = int(self.cs.get_register_def("FDATA7")['offset'], 16)
        self.fdata8_off = int(self.cs.get_register_def("FDATA8")['offset'], 16)
        self.fdata9_off = int(self.cs.get_register_def("FDATA9")['offset'], 16)
        self.fdata10_off = int(self.cs.get_register_def("FDATA10")['offset'], 16)
        self.fdata11_off = int(self.cs.get_register_def("FDATA11")['offset'], 16)
        self.fdata12_off = int(self.cs.get_register_def("FDATA12")['offset'], 16)
        self.fdata13_off = int(self.cs.get_register_def("FDATA13")['offset'], 16)
        self.fdata14_off = int(self.cs.get_register_def("FDATA14")['offset'], 16)
        self.fdata15_off = int(self.cs.get_register_def("FDATA15")['offset'], 16)
        self.bios_ptinx = int(self.cs.get_register_def("BIOS_PTINX")['offset'], 16)
        self.bios_ptdata = int(self.cs.get_register_def("BIOS_PTDATA")['offset'], 16)

        self.logger.log_hal("[spi] Reading SPI flash controller registers definitions:")
        self.logger.log_hal("      HSFC   offset = 0x{:04X}".format(self.hsfc_off))
        self.logger.log_hal("      HSFS   offset = 0x{:04X}".format(self.hsfs_off))
        self.logger.log_hal("      FADDR  offset = 0x{:04X}".format(self.faddr_off))
        self.logger.log_hal("      FDATA0 offset = 0x{:04X}".format(self.fdata0_off))

    def get_SPI_MMIO_base(self):
        (spi_base, spi_size) = self.cs.mmio.get_MMIO_BAR_base_address('SPIBAR')
        self.logger.log_hal("[spi] SPI MMIO base: 0x{:016X} (assuming below 4GB)".format(spi_base))
        return spi_base

    def spi_reg_read(self, reg, size=4):
        return self.cs.mmio.read_MMIO_reg(self.rcba_spi_base, reg, size)

    def spi_reg_write(self, reg, value, size=4):
        return self.cs.mmio.write_MMIO_reg(self.rcba_spi_base, reg, value, size)

    def get_SPI_region(self, spi_region_id):
        freg_name = SPI_REGION[spi_region_id]
        if not self.cs.is_register_defined(freg_name):
            return (None, None, None)
        freg = self.cs.read_register(freg_name)
        # Region Base corresponds to FLA bits 24:12
        range_base = self.cs.get_register_field(freg_name, freg, 'RB') << SPI_FLA_SHIFT
        # Region Limit corresponds to FLA bits 24:12
        range_limit = self.cs.get_register_field(freg_name, freg, 'RL') << SPI_FLA_SHIFT
        # FLA bits 11:0 are assumed to be FFFh for the limit comparison
        range_limit |= SPI_FLA_PAGE_MASK
        return (range_base, range_limit, freg)

    # all_regions = True : return all SPI regions
    # all_regions = False: return only available SPI regions (limit >= base)
    def get_SPI_regions(self, all_regions=True):
        spi_regions = {}
        for r in SPI_REGION:
            (range_base, range_limit, freg) = self.get_SPI_region(r)
            if range_base is None:
                continue
            if all_regions or (range_limit >= range_base):
                range_size = range_limit - range_base + 1
                spi_regions[r] = (range_base, range_limit, range_size, SPI_REGION_NAMES[r], freg)
        return spi_regions

    def get_SPI_Protected_Range(self, pr_num):
        if pr_num > SPI_MAX_PR_COUNT:
            return None

        pr_name = 'PR{:x}'.format(pr_num)
        pr_j_reg = int(self.cs.get_register_def(pr_name)['offset'], 16)
        pr_j = self.cs.read_register(pr_name)

        # Protected Range Base corresponds to FLA bits 24:12
        base = self.cs.get_register_field(pr_name, pr_j, 'PRB') << SPI_FLA_SHIFT
        # Protected Range Limit corresponds to FLA bits 24:12
        limit = self.cs.get_register_field(pr_name, pr_j, 'PRL') << SPI_FLA_SHIFT

        wpe = (0 != self.cs.get_register_field(pr_name, pr_j, 'WPE'))
        rpe = (0 != self.cs.get_register_field(pr_name, pr_j, 'RPE'))

        # Check if this is a valid PRx config
        if wpe or rpe:
            # FLA bits 11:0 are assumed to be FFFh for the limit comparison
            limit |= SPI_FLA_PAGE_MASK

        return (base, limit, wpe, rpe, pr_j_reg, pr_j)

    ##############################################################################################################
    # SPI configuration
    ##############################################################################################################

    def display_SPI_Flash_Descriptor(self):
        self.logger.log("============================================================")
        self.logger.log("SPI Flash Descriptor")
        self.logger.log("------------------------------------------------------------")
        self.logger.log("\nFlash Signature and Descriptor Map:")
        for j in range(5):
            self.cs.write_register('FDOC', (PCH_RCBA_SPI_FDOC_FDSS_FSDM | (j << 2)))
            fdod = self.cs.read_register('FDOD')
            self.logger.log("{:08X}".format(fdod))

        self.logger.log("\nComponents:")
        for j in range(3):
            self.cs.write_register('FDOC', (PCH_RCBA_SPI_FDOC_FDSS_COMP | (j << 2)))
            fdod = self.cs.read_register('FDOD')
            self.logger.log("{:08X}".format(fdod))

        self.logger.log("\nRegions:")
        for j in range(5):
            self.cs.write_register('FDOC', (PCH_RCBA_SPI_FDOC_FDSS_REGN | (j << 2)))
            fdod = self.cs.read_register('FDOD')
            self.logger.log("{:08X}".format(fdod))

        self.logger.log("\nMasters:")
        for j in range(3):
            self.cs.write_register('FDOC', (PCH_RCBA_SPI_FDOC_FDSS_MSTR | (j << 2)))
            fdod = self.cs.read_register('FDOD')
            self.logger.log("{:08X}".format(fdod))

    def display_SPI_opcode_info(self):
        self.logger.log("============================================================")
        self.logger.log("SPI Opcode Info")
        self.logger.log("------------------------------------------------------------")
        preop = self.cs.read_register('PREOP')
        self.logger.log("PREOP : 0x{:04X}".format(preop))
        optype = self.cs.read_register('OPTYPE')
        self.logger.log("OPTYPE: 0x{:04X}".format(optype))
        opmenu_lo = self.cs.read_register('OPMENU_LO')
        opmenu_hi = self.cs.read_register('OPMENU_HI')
        opmenu = ((opmenu_hi << 32) | opmenu_lo)
        self.logger.log("OPMENU: 0x{:016X}".format(opmenu))
        self.logger.log('')
        preop0 = preop & 0xFF
        preop1 = (preop >> 8) & 0xFF
        self.logger.log("Prefix Opcode 0 = 0x{:02X}".format(preop0))
        self.logger.log("Prefix Opcode 1 = 0x{:02X}".format(preop1))

        self.logger.log("------------------------------------------------------------")
        self.logger.log("Opcode # | Opcode | Optype | Description")
        self.logger.log("------------------------------------------------------------")
        for j in range(8):
            optype_j = ((optype >> j * 2) & 0x3)
            if (PCH_RCBA_SPI_OPTYPE_RDNOADDR == optype_j):
                desc = 'SPI read cycle without address'
            elif (PCH_RCBA_SPI_OPTYPE_WRNOADDR == optype_j):
                desc = 'SPI write cycle without address'
            elif (PCH_RCBA_SPI_OPTYPE_RDADDR == optype_j):
                desc = 'SPI read cycle with address'
            elif (PCH_RCBA_SPI_OPTYPE_WRADDR == optype_j):
                desc = 'SPI write cycle with address'
            self.logger.log("Opcode{:d}  | 0x{:02X}   | {:x}      | {} ".format(j, ((opmenu >> j * 8) & 0xFF), optype_j, desc))

    def display_SPI_Flash_Regions(self):
        self.logger.log("------------------------------------------------------------")
        self.logger.log("Flash Region             | FREGx Reg | Base     | Limit     ")
        self.logger.log("------------------------------------------------------------")
        regions = self.get_SPI_regions()
        for (region_id, region) in regions.items():
            base, limit, size, name, freg = region
            self.logger.log('{:d} {:22} | {:08X}  | {:08X} | {:08X} '.format(region_id, name, freg, base, limit))

    def display_BIOS_region(self):
        bfpreg = self.cs.read_register('BFPR')
        base = self.cs.get_register_field('BFPR', bfpreg, 'PRB') << SPI_FLA_SHIFT
        limit = self.cs.get_register_field('BFPR', bfpreg, 'PRL') << SPI_FLA_SHIFT
        limit |= SPI_FLA_PAGE_MASK
        self.logger.log("BIOS Flash Primary Region")
        self.logger.log("------------------------------------------------------------")
        self.logger.log("BFPREG = {:08X}:".format(bfpreg))
        self.logger.log("  Base  : {:08X}".format(base))
        self.logger.log("  Limit : {:08X}".format(limit))

    def display_SPI_Ranges_Access_Permissions(self):
        self.logger.log("SPI Flash Region Access Permissions")
        self.logger.log("------------------------------------------------------------")
        fracc = self.cs.read_register('FRAP')
        if self.logger.HAL:
            self.cs.print_register('FRAP', fracc)
        brra = self.cs.get_register_field('FRAP', fracc, 'BRRA')
        brwa = self.cs.get_register_field('FRAP', fracc, 'BRWA')
        bmrag = self.cs.get_register_field('FRAP', fracc, 'BMRAG')
        bmwag = self.cs.get_register_field('FRAP', fracc, 'BMWAG')
        self.logger.log('')
        self.logger.log("BIOS Region Write Access Grant ({:02X}):".format(bmwag))
        regions = self.get_SPI_regions()
        for region_id in regions:
            self.logger.log("  {:12}: {:1d}".format(SPI_REGION[region_id], (0 != bmwag & (1 << region_id))))
        self.logger.log("BIOS Region Read Access Grant ({:02X}):".format(bmrag))
        for region_id in regions:
            self.logger.log("  {:12}: {:1d}".format(SPI_REGION[region_id], (0 != bmrag & (1 << region_id))))
        self.logger.log("BIOS Region Write Access ({:02X}):".format(brwa))
        for region_id in regions:
            self.logger.log("  {:12}: {:1d}".format(SPI_REGION[region_id], (0 != brwa & (1 << region_id))))
        self.logger.log("BIOS Region Read Access ({:02X}):".format(brra))
        for region_id in regions:
            self.logger.log("  {:12}: {:1d}".format(SPI_REGION[region_id], (0 != brra & (1 << region_id))))

    def display_SPI_Protected_Ranges(self):
        self.logger.log("SPI Protected Ranges")
        self.logger.log("------------------------------------------------------------")
        self.logger.log("PRx (offset) | Value    | Base     | Limit    | WP? | RP?")
        self.logger.log("------------------------------------------------------------")
        for j in range(5):
            (base, limit, wpe, rpe, pr_reg_off, pr_reg_value) = self.get_SPI_Protected_Range(j)
            self.logger.log("PR{:d} ({:02X})     | {:08X} | {:08X} | {:08X} | {:d}   | {:d} ".format(j, pr_reg_off, pr_reg_value, base, limit, wpe, rpe))

    def display_SPI_map(self):
        self.logger.log("============================================================")
        self.logger.log("SPI Flash Map")
        self.logger.log("------------------------------------------------------------")
        self.logger.log('')
        self.display_BIOS_region()
        self.logger.log('')
        self.display_SPI_Flash_Regions()
        self.logger.log('')
        self.display_SPI_Flash_Descriptor()
        self.logger.log('')
        try:
            self.display_SPI_opcode_info()
        except Exception:
            pass
        self.logger.log('')
        self.logger.log("============================================================")
        self.logger.log("SPI Flash Protection")
        self.logger.log("------------------------------------------------------------")
        self.logger.log('')
        self.display_SPI_Ranges_Access_Permissions()
        self.logger.log('')
        self.logger.log("BIOS Region Write Protection")
        self.logger.log("------------------------------------------------------------")
        self.display_BIOS_write_protection()
        self.logger.log('')
        self.display_SPI_Protected_Ranges()
        self.logger.log('')

    ##############################################################################################################
    # BIOS Write Protection
    ##############################################################################################################
    def display_BIOS_write_protection(self):
        if self.cs.is_register_defined('BC'):
            reg_value = self.cs.read_register('BC')
            self.cs.print_register('BC', reg_value)
        else:
            self.logger.log_hal("Could not locate the definition of 'BIOS Control' register..")

    def disable_BIOS_write_protection(self):
        if self.logger.HAL:
            self.display_BIOS_write_protection()
        ble = self.cs.get_control('BiosLockEnable')
        bioswe = self.cs.get_control('BiosWriteEnable')
        smmbwp = self.cs.get_control('SmmBiosWriteProtection')

        if smmbwp == 1:
            self.logger.log_hal("[spi] SMM BIOS write protection (SmmBiosWriteProtection) is enabled")

        if bioswe == 1:
            self.logger.log_hal("[spi] BIOS write protection (BiosWriteEnable) is not enabled")
            return True
        elif ble == 0:
            self.logger.log_hal("[spi] BIOS write protection is enabled but not locked. Disabling..")
        else:  # bioswe == 0 and ble == 1
            self.logger.log_hal("[spi] BIOS write protection is enabled. Attempting to disable..")

        # Set BiosWriteEnable control bit
        self.cs.set_control('BiosWriteEnable', 1)

        # read BiosWriteEnable back to check if BIOS writes are enabled
        bioswe = self.cs.get_control('BiosWriteEnable')

        if self.logger.HAL:
            self.display_BIOS_write_protection()
        self.logger.log_hal("BIOS write protection is {} (BiosWriteEnable = {:d})".format('disabled' if bioswe else 'still enabled', bioswe))

        return (bioswe == 1)

    ##############################################################################################################
    # SPI Controller access functions
    ##############################################################################################################
    def _wait_SPI_flash_cycle_done(self):
        self.logger.log_hal("[spi] wait for SPI cycle ready/done..")

        for i in range(1000):
            hsfsts = self.spi_reg_read(self.hsfs_off, 1)

            cycle_done = not (hsfsts & PCH_RCBA_SPI_HSFSTS_SCIP)
            if cycle_done:
                break

        if not cycle_done:
            self.logger.log_hal("[spi] SPI cycle still in progress. Waiting 0.1 sec..")
            time.sleep(0.1)
            hsfsts = self.spi_reg_read(self.hsfs_off, 1)
            cycle_done = not (hsfsts & PCH_RCBA_SPI_HSFSTS_SCIP)

        if cycle_done:
            self.logger.log_hal("[spi] clear FDONE/FCERR/AEL bits..")
            self.spi_reg_write(self.hsfs_off, HSFSTS_CLEAR, 1)
            hsfsts = self.spi_reg_read(self.hsfs_off, 1)
            cycle_done = not ((hsfsts & PCH_RCBA_SPI_HSFSTS_AEL) or (hsfsts & PCH_RCBA_SPI_HSFSTS_FCERR))

        self.logger.log_hal("[spi] HSFS: 0x{:02X}".format(hsfsts))

        return cycle_done

    def _send_spi_cycle(self, hsfctl_spi_cycle_cmd, dbc, spi_fla):
        self.logger.log_hal("[spi] > send SPI cycle 0x{:x} to address 0x{:08X}..".format(hsfctl_spi_cycle_cmd, spi_fla))

        # No need to check for SPI cycle DONE status before each cycle
        # DONE status is checked once before entire SPI operation

        self.spi_reg_write(self.faddr_off, (spi_fla & PCH_RCBA_SPI_FADDR_MASK))
        # Other options ;)
        # chipsec.chipset.write_register(self.cs, "FADDR", (spi_fla & Cfg.PCH_RCBA_SPI_FADDR_MASK))
        # write_MMIO_reg(self.cs, spi_base, self.faddr_off, (spi_fla & Cfg.PCH_RCBA_SPI_FADDR_MASK))
        # self.cs.mem.write_physical_mem_dword(spi_base + self.faddr_off, (spi_fla & Cfg.PCH_RCBA_SPI_FADDR_MASK))

        if self.logger.HAL:
            _faddr = self.spi_reg_read(self.faddr_off)
            self.logger.log_hal("[spi] FADDR: 0x{:08X}".format(_faddr))

        self.logger.log_hal("[spi] SPI cycle GO (DBC <- 0x{:02X}, HSFC <- 0x{:x})".format(dbc, hsfctl_spi_cycle_cmd))

        if (HSFCTL_ERASE_CYCLE != hsfctl_spi_cycle_cmd):
            self.spi_reg_write(self.hsfc_off + 0x1, dbc, 1)

        self.spi_reg_write(self.hsfc_off, hsfctl_spi_cycle_cmd, 1)

        # Read HSFC back (logging only)
        if self.logger.HAL:
            _hsfc = self.spi_reg_read(self.hsfc_off, 1)
            self.logger.log_hal("[spi] HSFC: 0x{:04X}".format(_hsfc))

        cycle_done = self._wait_SPI_flash_cycle_done()
        if not cycle_done:
            self.logger.log_warning("SPI cycle not done")
        else:
            self.logger.log_hal("[spi] < SPI cycle done")

        return cycle_done

    def check_hardware_sequencing(self):
        # Test if the flash decriptor is valid (and hardware sequencing enabled)
        fdv = self.cs.read_register_field('HSFS', 'FDV')
        if fdv == 0:
            self.logger.error("HSFS.FDV is 0, hardware sequencing is disabled")
            raise SpiRuntimeError("Chipset does not support hardware sequencing")

    #
    # SPI Flash operations
    #

    def read_spi_to_file(self, spi_fla, data_byte_count, filename):
        buf = self.read_spi(spi_fla, data_byte_count)
        if buf is None:
            return None
        if filename is not None:
            write_file(filename, buf)
        else:
            print_buffer(buf, 16)
        return buf

    def write_spi_from_file(self, spi_fla, filename):
        buf = read_file(filename)
        return self.write_spi(spi_fla, struct.unpack('c' * len(buf), buf))

    def read_spi(self, spi_fla, data_byte_count):

        self.check_hardware_sequencing()

        buf = bytearray()
        dbc = SPI_READ_WRITE_DEF_DBC
        if (data_byte_count >= SPI_READ_WRITE_MAX_DBC):
            dbc = SPI_READ_WRITE_MAX_DBC

        n = data_byte_count // dbc
        r = data_byte_count % dbc
        if self.logger.UTIL_TRACE:
            self.logger.log_hal("[spi] reading 0x{:x} bytes from SPI at FLA = 0x{:x} (in {:d} 0x{:x}-byte chunks + 0x{:x}-byte remainder)".format(data_byte_count, spi_fla, n, dbc, r))

        cycle_done = self._wait_SPI_flash_cycle_done()
        if not cycle_done:
            self.logger.error("SPI cycle not ready")
            return None

        for i in range(n):
            self.logger.log_hal("[spi] reading chunk {:d} of 0x{:x} bytes from 0x{:x}".format(i, dbc, spi_fla + i * dbc))
            if not self._send_spi_cycle(HSFCTL_READ_CYCLE, dbc - 1, spi_fla + i * dbc):
                self.logger.error("SPI flash read failed")
            else:
                for fdata_idx in range(0, dbc // 4):
                    dword_value = self.spi_reg_read(self.fdata0_off + fdata_idx * 4)
                    self.logger.log_hal("[spi] FDATA00 + 0x{:x}: 0x{:x}".format(fdata_idx * 4, dword_value))
                    buf += struct.pack("I", dword_value)

        if (0 != r):
            self.logger.log_hal("[spi] reading remaining 0x{:x} bytes from 0x{:x}".format(r, spi_fla + n * dbc))
            if not self._send_spi_cycle(HSFCTL_READ_CYCLE, r - 1, spi_fla + n * dbc):
                self.logger.error("SPI flash read failed")
            else:
                t = 4
                n_dwords = (r + 3) // 4
                for fdata_idx in range(0, n_dwords):
                    dword_value = self.spi_reg_read(self.fdata0_off + fdata_idx * 4)
                    self.logger.log_hal("[spi] FDATA00 + 0x{:x}: 0x{:08X}".format(fdata_idx * 4, dword_value))
                    if (fdata_idx == (n_dwords - 1)) and (0 != r % 4):
                        t = r % 4
                    for j in range(t):
                        buf += struct.pack('B', (dword_value >> (8 * j)) & 0xff)

        self.logger.log_hal("[spi] buffer read from SPI:")
        if self.logger.HAL:
            print_buffer_bytes(buf)

        return buf

    def write_spi(self, spi_fla, buf):

        self.check_hardware_sequencing()

        write_ok = True
        data_byte_count = len(buf)
        dbc = 4
        n = data_byte_count // dbc
        r = data_byte_count % dbc
        if self.logger.UTIL_TRACE:
            self.logger.log_hal("[spi] writing 0x{:x} bytes to SPI at FLA = 0x{:x} (in {:d} 0x{:x}-byte chunks + 0x{:x}-byte remainder)".format(data_byte_count, spi_fla, n, dbc, r))

        cycle_done = self._wait_SPI_flash_cycle_done()
        if not cycle_done:
            self.logger.error("SPI cycle not ready")
            return None

        for i in range(n):
            if self.logger.UTIL_TRACE:
                self.logger.log_hal("[spi] writing chunk {:d} of 0x{:x} bytes to 0x{:x}".format(i, dbc, spi_fla + i * dbc))
            dword_value = (ord(buf[i * dbc + 3]) << 24) | (ord(buf[i * dbc + 2]) << 16) | (ord(buf[i * dbc + 1]) << 8) | ord(buf[i * dbc])
            self.logger.log_hal("[spi] in FDATA00 = 0x{:08X}".format(dword_value))
            self.spi_reg_write(self.fdata0_off, dword_value)
            if not self._send_spi_cycle(HSFCTL_WRITE_CYCLE, dbc - 1, spi_fla + i * dbc):
                write_ok = False
                self.logger.error("SPI flash write cycle failed")

        if (0 != r):
            if self.logger.UTIL_TRACE:
                self.logger.log_hal("[spi] writing remaining 0x{:x} bytes to FLA = 0x{:x}".format(r, spi_fla + n * dbc))
            dword_value = 0
            for j in range(r):
                dword_value |= (ord(buf[n * dbc + j]) << 8 * j)
            self.logger.log_hal("[spi] in FDATA00 = 0x{:08X}".format(dword_value))
            self.spi_reg_write(self.fdata0_off, dword_value)
            if not self._send_spi_cycle(HSFCTL_WRITE_CYCLE, r - 1, spi_fla + n * dbc):
                write_ok = False
                self.logger.error("SPI flash write cycle failed")

        return write_ok

    def erase_spi_block(self, spi_fla):

        self.check_hardware_sequencing()

        if self.logger.UTIL_TRACE:
            self.logger.log_hal("[spi] Erasing SPI Flash block @ 0x{:x}".format(spi_fla))

        cycle_done = self._wait_SPI_flash_cycle_done()
        if not cycle_done:
            self.logger.error("SPI cycle not ready")
            return None

        erase_ok = self._send_spi_cycle(HSFCTL_ERASE_CYCLE, 0, spi_fla)
        if not erase_ok:
            self.logger.error("SPI Flash erase cycle failed")

        return erase_ok

    #
    # SPI SFDP operations
    #
    def ptmesg(self, offset):
        self.spi_reg_write(self.bios_ptinx, offset)
        self.spi_reg_read(self.bios_ptinx)
        return self.spi_reg_read(self.bios_ptdata)

    def get_SPI_SFDP(self):
        ret = False
        for component in range(0, 2):
            self.logger.log("Scanning for Flash device {:d}".format(component + 1))
            offset = 0x0000 | (component << 14)
            sfdp_signature = self.ptmesg(offset)
            if sfdp_signature == SFDP_HEADER:
                self.logger.log("  * Found valid SFDP header for Flash device {:d}".format(component + 1))
                ret = True
            else:
                self.logger.log("  * Didn't find a valid SFDP header for Flash device {:d}".format(component + 1))
                continue
            # Increment offset to read second dword of SFDP header structure
            sfdp_data = self.ptmesg(offset + 0x4)
            sfdp_minor_version = sfdp_data & 0xFF
            sfdp_major_version = (sfdp_data >> 8) & 0xFF
            self.logger.log("    SFDP version number: {}.{}".format(sfdp_major_version, sfdp_minor_version))
            num_of_param_headers = ((sfdp_data >> 16) & 0xFF) + 1
            self.logger.log("    Number of parameter headers: {:d}".format(num_of_param_headers))
            # Set offset to read 1st Parameter Table in the SFDP header structure
            offset = offset | 0x1000
            parameter_1 = self.ptmesg(offset)
            param1_minor_version = (parameter_1 >> 8) & 0xFF
            param1_major_version = (parameter_1 >> 16) & 0xFF
            param1_length = (parameter_1 >> 24) & 0xFF
            self.logger.log("  * Parameter Header 1 (JEDEC)")
            self.logger.log("    ** Parameter version number: {}.{}".format(param1_major_version, param1_minor_version))
            self.logger.log("    ** Parameter length in double words: {}".format(hex(param1_length)))
            if (num_of_param_headers > 1) and self.cs.register_has_field('HSFS', 'FCYCLE'):
                self.check_hardware_sequencing()
                self.spi_reg_write(self.fdata12_off, 0x00000000)
                self.spi_reg_write(self.fdata13_off, 0x00000000)
                self.spi_reg_write(self.fdata14_off, 0x00000000)
                self.spi_reg_write(self.fdata15_off, 0x00000000)
                if not self._send_spi_cycle(HSFCTL_SFDP_CYCLE, 0x3F, 0):
                    self.logger.error('SPI SFDP signature cycle failed')
                    continue
                pTable_offset_list = []
                pTable_length = []
                # Calculate which fdata_offset registers to read, based on number of parameter headers present
                for i in range(1, num_of_param_headers):
                    self.logger.log("  * Parameter Header:{:d}".format(i + 1))
                    data_reg_1 = "self.fdata" + str(2 + (2 * i)) + "_off"
                    data_reg_2 = "self.fdata" + str(2 + (2 * i) + 1) + "_off"
                    data_dword_1 = self.spi_reg_read(eval(data_reg_1))
                    data_dword_2 = self.spi_reg_read(eval(data_reg_2))
                    id_manuf = (data_dword_2 & 0xFF000000) >> 16 | (data_dword_1 & 0xFF)
                    param_minor_version = (data_dword_1 >> 8) & 0xFF
                    param_major_version = (data_dword_1 >> 16) & 0xFF
                    param_length = (data_dword_1 >> 24) & 0xFF
                    param_table_pointer = (data_dword_2 & 0x00FFFFFF)
                    self.logger.log("    ** Parameter version number:{}.{}".format(param_major_version, param_minor_version))
                    self.logger.log("    ** Pramaeter length in double words: {}".format(hex(param_length)))
                    self.logger.log("    ** Parameter ID: {}".format(hex(id_manuf)))
                    self.logger.log("    ** Parameter Table Pointer(byte address): {} ".format(hex(param_table_pointer)))
                    pTable_offset_list.append(param_table_pointer)
                    pTable_length.append(param_length)
            offset = 0x0000 | (component << 14)
            # Set offset to read 1st Parameter table (JEDEC Basic Flash Parameter Table) content and Parse it
            offset = offset | 0x2000
            self.logger.log("                                ")
            self.logger.log("  * 1'st Parameter Table Content ")
            for count in range(1, param1_length + 1):
                sfdp_data = self.ptmesg(offset)
                offset += 4
                self.cs.print_register("DWORD{}".format(count), sfdp_data)
        return ret

    #
    # SPI JEDEC ID operations
    #
    def get_SPI_JEDEC_ID(self):

        if self.cs.register_has_field('HSFS', 'FCYCLE'):
            self.check_hardware_sequencing()

            if not self._send_spi_cycle(HSFCTL_JEDEC_CYCLE, 4, 0):
                self.logger.error('SPI JEDEC ID cycle failed')
            id = self.spi_reg_read(self.fdata0_off)
        else:
            return False

        return ((id & 0xFF) << 16) | (id & 0xFF00) | ((id >> 16) & 0xFF)

    def get_SPI_JEDEC_ID_decoded(self):

        jedec_id = self.get_SPI_JEDEC_ID()
        if jedec_id is False:
            return (False, 0, 0)
        manu = JEDEC_ID.MANUFACTURER.get((jedec_id >> 16) & 0xff, 'Unknown')
        part = JEDEC_ID.DEVICE.get(jedec_id, 'Unknown')

        return (jedec_id, manu, part)

    def parse_spi_flash_descriptor(self, rom):
        if not (isinstance(rom, str) or isinstance(rom, bytes)):
            self.logger.error('Invalid fd object type {}'.format(type(rom)))
            return

        pos = rom.find(SPI_FLASH_DESCRIPTOR_SIGNATURE)
        if (-1 == pos or pos < 0x10):
            self.logger.error('Valid SPI flash descriptor is not found (should have signature {:08X})'.format(struct.unpack('=I', SPI_FLASH_DESCRIPTOR_SIGNATURE)[0]))
            return None

        fd_off = pos - 0x10
        self.logger.log('[spi_fd] Valid SPI flash descriptor found at offset 0x{:08X}'.format(fd_off))

        self.logger.log('')
        self.logger.log('########################################################')
        self.logger.log('# SPI FLASH DESCRIPTOR')
        self.logger.log('########################################################')
        self.logger.log('')

        fd = rom[fd_off: fd_off + SPI_FLASH_DESCRIPTOR_SIZE]
        fd_sig = struct.unpack_from('=I', fd[0x10:0x14])[0]

        self.logger.log('+ 0x0000 Reserved : 0x{}'.format(hexlify(fd[0x0:0xF]).upper()))
        self.logger.log('+ 0x0010 Signature: 0x{:08X}'.format(fd_sig))

        #
        # Flash Descriptor Map Section
        #
        flmap0 = struct.unpack_from('=I', fd[0x14:0x18])[0]
        flmap1 = struct.unpack_from('=I', fd[0x18:0x1C])[0]
        flmap2 = struct.unpack_from('=I', fd[0x1C:0x20])[0]
        self.cs.print_register('FLMAP0', flmap0)
        self.cs.print_register('FLMAP1', flmap1)
        self.cs.print_register('FLMAP2', flmap2)

        fcba = self.cs.get_register_field('FLMAP0', flmap0, 'FCBA')
        nc = self.cs.get_register_field('FLMAP0', flmap0, 'NC')
        frba = self.cs.get_register_field('FLMAP0', flmap0, 'FRBA')
        fcba = fcba << 4
        frba = frba << 4
        nc += 1
        self.logger.log('')
        self.logger.log('+ 0x0014 Flash Descriptor Map:')
        self.logger.log('========================================================')
        self.logger.log('  Flash Component Base Address: 0x{:08X}'.format(fcba))
        self.logger.log('  Flash Region Base Address   : 0x{:08X}'.format(frba))
        self.logger.log('  Number of Flash Components  : {:d}'.format(nc))

        nr = SPI_REGION_NUMBER_IN_FD
        if self.cs.register_has_field('FLMAP0', 'NR'):
            nr = self.cs.get_register_field('FLMAP0', flmap0, 'NR')
            if nr == 0:
                self.logger.log_warning('only 1 region (FD) is found. Looks like flash descriptor binary is from Skylake platform or later. Try with option --platform')
            nr += 1
            self.logger.log('  Number of Regions           : {:d}'.format(nr))

        fmba = self.cs.get_register_field('FLMAP1', flmap1, 'FMBA')
        nm = self.cs.get_register_field('FLMAP1', flmap1, 'NM')
        fpsba = self.cs.get_register_field('FLMAP1', flmap1, 'FPSBA')
        psl = self.cs.get_register_field('FLMAP1', flmap1, 'PSL')
        fmba = fmba << 4
        fpsba = fpsba << 4
        self.logger.log('  Flash Master Base Address   : 0x{:08X}'.format(fmba))
        self.logger.log('  Number of Masters           : {:d}'.format(nm))
        self.logger.log('  Flash PCH Strap Base Address: 0x{:08X}'.format(fpsba))
        self.logger.log('  PCH Strap Length            : 0x{:X}'.format(psl))

        fcpusba = self.cs.get_register_field('FLMAP2', flmap2, 'FCPUSBA')
        cpusl = self.cs.get_register_field('FLMAP2', flmap2, 'CPUSL')
        self.logger.log('  Flash CPU Strap Base Address: 0x{:08X}'.format(fcpusba))
        self.logger.log('  CPU Strap Length            : 0x{:X}'.format(cpusl))

        #
        # Flash Descriptor Component Section
        #
        self.logger.log('')
        self.logger.log('+ 0x{:04X} Component Section:'.format(fcba))
        self.logger.log('========================================================')

        flcomp = struct.unpack_from('=I', fd[fcba + 0x0:fcba + 0x4])[0]
        self.logger.log('+ 0x{:04X} FLCOMP   : 0x{:08X}'.format(fcba, flcomp))
        flil = struct.unpack_from('=I', fd[fcba + 0x4:fcba + 0x8])[0]
        self.logger.log('+ 0x{:04X} FLIL     : 0x{:08X}'.format(fcba + 0x4, flil))
        flpb = struct.unpack_from('=I', fd[fcba + 0x8:fcba + 0xC])[0]
        self.logger.log('+ 0x{:04X} FLPB     : 0x{:08X}'.format(fcba + 0x8, flpb))

        #
        # Flash Descriptor Region Section
        #
        self.logger.log('')
        self.logger.log('+ 0x{:04X} Region Section:'.format(frba))
        self.logger.log('========================================================')

        flregs = [None] * nr
        for r in range(nr):
            flreg_off = frba + r * 4
            flreg = struct.unpack_from('=I', fd[flreg_off:flreg_off + 0x4])[0]
            if not self.cs.is_register_defined('FLREG{:d}'.format(r)):
                continue
            base = self.cs.get_register_field(('FLREG{:d}'.format(r)), flreg, 'RB') << SPI_FLA_SHIFT
            limit = self.cs.get_register_field(('FLREG{:d}'.format(r)), flreg, 'RL') << SPI_FLA_SHIFT
            notused = '(not used)' if base > limit or flreg == 0xFFFFFFFF else ''
            flregs[r] = (flreg, base, limit, notused)
            self.logger.log('+ 0x{:04X} FLREG{:d}   : 0x{:08X} {}'.format(flreg_off, r, flreg, notused))

        self.logger.log('')
        self.logger.log('Flash Regions')
        self.logger.log('--------------------------------------------------------')
        self.logger.log(' Region                | FLREGx    | Base     | Limit   ')
        self.logger.log('--------------------------------------------------------')
        for r in range(nr):
            if flregs[r]:
                self.logger.log('{:d} {:20s} | {:08X}  | {:08X} | {:08X} {}'.format(r, SPI_REGION_NAMES[r], flregs[r][0], flregs[r][1], flregs[r][2], flregs[r][3]))

        #
        # Flash Descriptor Master Section
        #
        self.logger.log('')
        self.logger.log('+ 0x{:04X} Master Section:'.format(fmba))
        self.logger.log('========================================================')

        flmstrs = [None] * nm
        for m in range(nm):
            flmstr_off = fmba + m * 4
            flmstr = struct.unpack_from('=I', fd[flmstr_off:flmstr_off + 0x4])[0]
            master_region_ra = self.cs.get_register_field('FLMSTR1', flmstr, 'MRRA')
            master_region_wa = self.cs.get_register_field('FLMSTR1', flmstr, 'MRWA')
            flmstrs[m] = (master_region_ra, master_region_wa)
            self.logger.log('+ 0x{:04X} FLMSTR{:d}   : 0x{:08X}'.format(flmstr_off, m + 1, flmstr))

        self.logger.log('')
        self.logger.log('Master Read/Write Access to Flash Regions')
        self.logger.log('--------------------------------------------------------')
        s = ' Region                 '
        for m in range(nm):
            if m in SPI_MASTER_NAMES:
                s = s + '| ' + ('{:9}'.format(SPI_MASTER_NAMES[m]))
            else:
                s = s + '| Master {:-2d}'.format(m)
        self.logger.log(s)
        self.logger.log('--------------------------------------------------------')
        for r in range(nr):
            s = '{:-2d} {:20s} '.format(r, SPI_REGION_NAMES[r])
            for m in range(nm):
                access_s = ''
                mask = (0x1 << r)
                if (flmstrs[m][0] & mask):
                    access_s += 'R'
                if (flmstrs[m][1] & mask):
                    access_s += 'W'
                s = s + '| ' + ('{:9}'.format(access_s))
            self.logger.log(s)

        #
        # Flash Descriptor Upper Map Section
        #
        self.logger.log('')
        self.logger.log('+ 0x{:04X} Flash Descriptor Upper Map:'.format(0xEFC))
        self.logger.log('========================================================')

        flumap1 = struct.unpack_from('=I', fd[0xEFC:0xF00])[0]
        self.logger.log('+ 0x{:04X} FLUMAP1   : 0x{:08X}'.format(0xEFC, flumap1))

        vtba = ((flumap1 & 0x000000FF) << 4)
        vtl = (((flumap1 & 0x0000FF00) >> 8) & 0xFF)
        self.logger.log('  VSCC Table Base Address    = 0x{:08X}'.format(vtba))
        self.logger.log('  VSCC Table Length          = 0x{:02X}'.format(vtl))

        #
        # OEM Section
        #
        self.logger.log('')
        self.logger.log('+ 0x{:04X} OEM Section:'.format(0xF00))
        self.logger.log('========================================================')
        print_buffer_bytes(fd[0xF00:])

        self.logger.log('')
        self.logger.log('########################################################')
        self.logger.log('# END OF SPI FLASH DESCRIPTOR')
        self.logger.log('########################################################')
