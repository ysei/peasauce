"""
    Peasauce - interactive disassembler
    Copyright (C) 2012  Richard Tew

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""

DEBUG_ANNOTATE_DISASSEMBLY = True

import bisect
import logging
import os
import sys
import traceback


import archlib
from disasmlib import archm68k

logger = logging.getLogger()


# CURRENT GLOBAL VARIABLES
branch_addresses = None
file_info = None
symbols_by_address = None

file_metadata_addresses = None
file_metadata_line0s = None
file_metadata_blocks = None
file_metadata_dirtyidx = None

END_INSTRUCTION_LINES = 2


class DisplayConfiguration(object):
    trailing_line_exit = True
    trailing_line_branch = True
    trailing_line_trap = True

display_configuration = DisplayConfiguration()

class SegmentBlock(object):
    """ The number of this segment in the file. """
    segment_id = None
    """ The offset of this block in its segment. """
    segment_offset = None
    """ All segments appear as one contiguous address space.  This is the offset of this block in that space. """
    address = None
    """ The number of bytes data that this block contains. """
    length = None
    """ The data type of this block (DATA_TYPE_*) and more """
    flags = 0
    """ DATA_TYPE_CODE: [ line0_match, ... lineN_match ]. """
    line_data = None
    """ Calculated number of lines. """
    line_count = 0

## Utility functions

def _count_bits(v):
    count = 0
    while v:
        count += 1
        v >>= 1
    return count

def _make_bitmask(bitcount):
    mask = 0
    while bitcount:
        bitcount -= 1
        mask |= 1<<bitcount
    return mask

## SegmentBlock flag helpers

def get_data_type(flags):
    return (flags >> DATA_TYPE_BIT0) & DATA_TYPE_BITMASK

def set_data_type(block, data_type):
    block.flags &= ~(DATA_TYPE_BITMASK << DATA_TYPE_BIT0)
    block.flags |= ((data_type & DATA_TYPE_BITMASK) << DATA_TYPE_BIT0)


DATA_TYPE_CODE          = 1
DATA_TYPE_ASCII         = 2
DATA_TYPE_BYTE          = 3
DATA_TYPE_WORD          = 4
DATA_TYPE_LONGWORD      = 5
DATA_TYPE_BIT0          = DATA_TYPE_CODE - 1
DATA_TYPE_BITCOUNT      = _count_bits(DATA_TYPE_LONGWORD)
DATA_TYPE_BITMASK       = _make_bitmask(DATA_TYPE_BITCOUNT)

""" Indicates that the block is not backed by file data. """
BLOCK_FLAG_ALLOC        = 1 << (DATA_TYPE_BITCOUNT+0)

""" Indicates that the block has been processed. """
BLOCK_FLAG_PROCESSED    = 1 << (DATA_TYPE_BITCOUNT+1)

""" The mask for the flags to preserve if the block is split. """
BLOCK_SPLIT_BITMASK     = BLOCK_FLAG_ALLOC | DATA_TYPE_BITMASK

""" Used to map block data type to a character used in the label generation. """
char_by_data_type = { DATA_TYPE_CODE: "C", DATA_TYPE_ASCII: "A", DATA_TYPE_BYTE: "B", DATA_TYPE_WORD: "W", DATA_TYPE_LONGWORD: "L" }

def calculate_block_leading_line_count(block):
    if block.segment_offset == 0 and file_info.has_section_headers():
        return 1
    return 0

def calculate_match_line_count(match):
    line_count = 1
    if display_configuration.trailing_line_exit and archm68k.is_final_instruction(match):
        line_count += 1
    elif display_configuration.trailing_line_trap and match.specification.key == "TRAP":
        line_count += 1
    elif display_configuration.trailing_line_branch and match.specification.key in ("Bcc", "DBcc",):
        line_count += 1
    return line_count

def calculate_line_count(block):
    old_line_count = block.line_count
    block.line_count = calculate_block_leading_line_count(block)
    if get_data_type(block.flags) == DATA_TYPE_CODE:
        for match in block.line_data:
            block.line_count += calculate_match_line_count(match)
    elif get_data_type(block.flags) in (DATA_TYPE_LONGWORD, DATA_TYPE_WORD, DATA_TYPE_BYTE):
        # If there are excess bytes that do not fit into the given data type, append them in the smaller data types.
        if get_data_type(block.flags) == DATA_TYPE_LONGWORD:
            size_types = [ ("L", 4), ("W", 2), ("B", 1) ]
        elif get_data_type(block.flags) == DATA_TYPE_WORD:
            size_types = [ ("W", 2), ("B", 1) ]
        elif get_data_type(block.flags) == DATA_TYPE_BYTE:
            size_types = [ ("B", 1) ]

        size_counts = []
        excess_length = block.length
        for size_char, num_bytes in size_types:
            size_count = excess_length / num_bytes
            if size_count == 0:
                continue
            size_counts.append(size_count)
            excess_length -= size_count * num_bytes

        # Memory that is not mapped to file contents is placed into aggregate space declarations.
        if block.flags & BLOCK_FLAG_ALLOC:
            block.line_count += len(size_counts)
        else:
            block.line_count += sum(size_counts)
    else:
        block.line_count = 0
        return block.line_count - old_line_count

    # Last block in a segment gets a trailing line, if it is not the last segment.
    if block.segment_offset + block.length == file_info.get_segment_length(block.segment_id) and block.segment_id < file_info.get_segment_count()-1:
        block.line_count += 1 # SEGMENT FOOTER (blank line)
    return block.line_count - old_line_count

LI_OFFSET = 0
LI_BYTES = 1
LI_LABEL = 2
LI_INSTRUCTION = 3
LI_OPERANDS = 4
if DEBUG_ANNOTATE_DISASSEMBLY:
    LI_ANNOTATIONS = 5


def get_line_number_for_address(address):
    block, block_idx = lookup_metadata_by_address(address)
    base_address = file_metadata_addresses[block_idx]
    line_number0 = file_metadata_line0s[block_idx]
    line_number1 = line_number0 + block.line_count

    # Account for leading lines.
    line_number0 += calculate_block_leading_line_count(block)

    if get_data_type(block.flags) == DATA_TYPE_CODE:
        bytes_used = 0
        line_number = line_number0
        for match in block.line_data:
            if base_address + bytes_used == address:
                return line_number
            bytes_used += match.num_bytes
            line_number += calculate_match_line_count(match)
        logger.error("Miscounted lines for block")
        raise RuntimeError("xxx")
    elif get_data_type(block.flags) in (DATA_TYPE_LONGWORD, DATA_TYPE_WORD, DATA_TYPE_BYTE):
        if get_data_type(block.flags) == DATA_TYPE_LONGWORD:
            size_types = [ ("L", 4), ("W", 2), ("B", 1) ]
        elif get_data_type(block.flags) == DATA_TYPE_WORD:
            size_types = [ ("W", 2), ("B", 1) ]
        elif get_data_type(block.flags) == DATA_TYPE_BYTE:
            size_types = [ ("B", 1) ]

        line_address0 = base_address
        line_count0 = line_number0
        excess_length = block.length
        for size_char, num_bytes in size_types:
            size_count = excess_length / num_bytes
            if size_count > 0:
                #print size_count, line_number, (line_count0, size_count), line_count0 + size_count
                num_size_bytes = size_count * num_bytes
                if num_size_bytes <= excess_length:
                    return line_count0 + (address - line_address0) / num_bytes
                excess_length -= size_count * num_bytes
                line_address0 += num_size_bytes
                line_count0 += size_count

    return None

def get_address_for_line_number(line_number):
    block, block_idx = lookup_metadata_by_line_count(line_number)
    base_line_count = file_metadata_line0s[block_idx] + calculate_block_leading_line_count(block)
    address0 = file_metadata_addresses[block_idx]
    address1 = address0 + block.length
    if get_data_type(block.flags) == DATA_TYPE_CODE:
        bytes_used = 0
        line_count = base_line_count
        for match in block.line_data:
            if line_count == line_number:
                return address0 + bytes_used
            bytes_used += match.num_bytes
            line_count += calculate_match_line_count(match)
        logger.error("Miscounted address for block ()")
        raise RuntimeError("xxx")
    elif get_data_type(block.flags) in (DATA_TYPE_LONGWORD, DATA_TYPE_WORD, DATA_TYPE_BYTE):
        if get_data_type(block.flags) == DATA_TYPE_LONGWORD:
            size_types = [ ("L", 4), ("W", 2), ("B", 1) ]
        elif get_data_type(block.flags) == DATA_TYPE_WORD:
            size_types = [ ("W", 2), ("B", 1) ]
        elif get_data_type(block.flags) == DATA_TYPE_BYTE:
            size_types = [ ("B", 1) ]

        # 
        line_address0 = address0
        line_count0 = base_line_count
        excess_length = block.length
        for size_char, num_bytes in size_types:
            size_count = excess_length / num_bytes
            if size_count > 0:
                #print size_count, line_number, (line_count0, size_count), line_count0 + size_count
                if line_number < line_count0 + size_count:
                    return line_address0 + (line_number - line_count0) * num_bytes
                num_size_bytes = size_count * num_bytes
                excess_length -= size_count * num_bytes
                line_address0 += num_size_bytes
                line_count0 += size_count

    return None

def get_line_count():
    if file_metadata_line0s is None:
        return 0
    return file_metadata_line0s[-1] + file_metadata_blocks[-1].line_count + END_INSTRUCTION_LINES

def get_file_line(line_idx, column_idx): # Zero-based
    block, block_idx = lookup_metadata_by_line_count(line_idx)
    block_line_count0 = file_metadata_line0s[block_idx]
    block_line_countN = block_line_count0 + block.line_count
    
    # If the line is the first of the block, check if it is a segment header.
    leading_line_count = 0
    if block.segment_offset == 0 and file_info.has_section_headers():
        if line_idx == block_line_count0:
            section_header = file_info.get_section_header(block.segment_id)
            i = section_header.find(" ")
            if column_idx == LI_INSTRUCTION:
                return section_header[0:i]
            elif column_idx == LI_OPERANDS:
                return section_header[i+1:]
            else:
                return ""
        leading_line_count += 1

    # If the line is the last line in a block, check if it a "between segments" trailing blank line.
    if line_idx == block_line_countN-1:
        if block.segment_offset + block.length == file_info.get_segment_length(block.segment_id) and block.segment_id < file_info.get_segment_count()-1:
            return ""

    if get_data_type(block.flags) == DATA_TYPE_CODE:
        # block, block_idx = lookup_metadata_by_line_count(line_idx)
        # block_line_count0 = file_metadata_line0s[block_idx]
        bytes_used = 0
        line_count = block_line_count0 + leading_line_count
        line_match = None
        for match in block.line_data:
            if line_count == line_idx:
                line_match = match
                break
            bytes_used += match.num_bytes
            line_count += calculate_match_line_count(match)
        else:
            # Trailing blank lines.
            return ""
        address = block.address + bytes_used

        if column_idx == LI_OFFSET:
            return "%08X" % address
        elif column_idx == LI_BYTES:
            data = file_info.get_segment_data(block.segment_id)
            return "".join([ "%02X" % c for c in data[block.segment_offset+bytes_used:block.segment_offset+block.length-bytes_used] ])
        elif column_idx == LI_LABEL:
            label = lookup_address_label(address)
            if label is None:
                return ""
            return label
        elif column_idx == LI_INSTRUCTION:
            return archm68k.get_instruction_string(line_match, line_match.vars)
        elif column_idx == LI_OPERANDS:
            def make_operand_string(match, operand, operand_idx):
                operand_string = None
                if match.specification.key[0:4] != "LINK" and operand.specification.key == "DISPLACEMENT":
                    operand_string = lookup_address_label(match.pc + operand.vars["xxx"])
                elif operand.specification.key == "AbsL" or operand.key == "AbsL":
                    operand_string = lookup_address_label(operand.vars["xxx"])
                elif operand_idx == 0 and (operand.specification.key == "Imm" or operand.key == "Imm"):
                    # e.g. MOVEA.L #vvv, A0 ?
                    if len(match.opcodes) > 1:
                        operand2 = match.opcodes[1]
                        if (operand2.specification.key == "AR" or operand.key == "AR"):
                            operand_string = lookup_address_label(operand.vars["xxx"])
                            if operand_string is not None:
                                operand_string = "#"+ operand_string
                if operand_string is None:
                    return archm68k.get_operand_string(match.pc, operand, operand.vars, lookup_symbol=lookup_address_label)
                return operand_string
            opcode_string = ""
            if len(line_match.opcodes) >= 1:
                opcode_string += make_operand_string(line_match, line_match.opcodes[0], 0)
            if len(line_match.opcodes) == 2:
                opcode_string += ", "+ make_operand_string(line_match, line_match.opcodes[1], 1)
            return opcode_string
        elif DEBUG_ANNOTATE_DISASSEMBLY and column_idx == LI_ANNOTATIONS:
            l = []
            for o in line_match.opcodes:
                key = o.specification.key
                if o.key is not None and key != o.key:
                    l.append(o.key)
                else:
                    l.append(key)
            return line_match.specification.key +" "+ ",".join(l)
    elif get_data_type(block.flags) in (DATA_TYPE_LONGWORD, DATA_TYPE_WORD, DATA_TYPE_BYTE):
        # If there are excess bytes that do not fit into the given data type, append them in the smaller data types.
        size_types = []
        if get_data_type(block.flags) == DATA_TYPE_LONGWORD:
            size_types.append((4, "L", archm68k._get_long))
        if get_data_type(block.flags) in (DATA_TYPE_LONGWORD, DATA_TYPE_WORD):
            size_types.append((2, "W", archm68k._get_word))
        if get_data_type(block.flags) in (DATA_TYPE_LONGWORD, DATA_TYPE_WORD, DATA_TYPE_BYTE):
            size_types.append((1, "B", archm68k._get_byte))

        unconsumed_byte_count = block.length
        size_line_countN = block_line_count0 + leading_line_count
        for num_bytes, size_char, read_func in size_types:
            size_count = unconsumed_byte_count / num_bytes
            if size_count == 0:
                continue
            data_idx0 = block.segment_offset + (block.length - unconsumed_byte_count)
            unconsumed_byte_count -= size_count * num_bytes
            size_line_count0 = size_line_countN
            if block.flags & BLOCK_FLAG_ALLOC:
                size_line_countN += 1
            else:
                size_line_countN += size_count
            if line_idx < size_line_countN:
                data_idx = data_idx0 + (line_idx - size_line_count0) * num_bytes
                if column_idx == LI_OFFSET:
                    return "%08X" % (file_info.get_segment_address(block.segment_id) + data_idx)
                elif column_idx == LI_BYTES:
                    if block.flags & BLOCK_FLAG_ALLOC:
                        return ""
                    data = file_info.get_segment_data(block.segment_id)
                    return "".join([ "%02X" % c for c in data[data_idx:data_idx+num_bytes] ])
                elif column_idx == LI_LABEL:
                    label = lookup_address_label(file_info.get_segment_address(block.segment_id) + data_idx)
                    if label is None:
                        return ""
                    return label
                elif column_idx == LI_INSTRUCTION:
                    name = file_info.get_data_instruction_string(block.segment_id, (block.flags & BLOCK_FLAG_ALLOC) != BLOCK_FLAG_ALLOC)
                    return name +"."+ size_char
                elif column_idx == LI_OPERANDS:
                    if block.flags & BLOCK_FLAG_ALLOC:
                        return str(size_count)
                    data = file_info.get_segment_data(block.segment_id)
                    value = read_func(data, data_idx)[0]
                    label = None
                    if size_char == "L" and value in file_info.relocatable_addresses:
                        label = lookup_address_label(value)
                    if label is None:
                        label = ("$%0"+ str(num_bytes<<1) +"X") % value
                    return label
                elif DEBUG_ANNOTATE_DISASSEMBLY and column_idx == LI_ANNOTATIONS:
                    return "-"

    block_line_count0 = block_line_countN

    # Second to last line is a blank line.
    if line_idx == block_line_count0:
        return ""

    # Last line is an end instruction.
    if line_idx == block_line_count0+1:
        if column_idx == LI_INSTRUCTION:
            return "END"
        return ""

def lookup_address_label(address):
    if address in symbols_by_address:
        return symbols_by_address[address]

    format_string = "lbL%06X"
    if address in branch_addresses: # How to symbolize this?
        return format_string % address

def insert_branch_address(address, src_abs_idx):
    if address not in file_info.relocated_addresses:
        branch_addresses.add(address)
        # This errors because if the block is a code block, it has no match object.
        # split_block(address)

_symbol_insert_func = None
def set_symbol_insert_func(f):
    global _symbol_insert_func
    _symbol_insert_func = f

def insert_symbol(address, name):
    symbols_by_address[address] = name
    if _symbol_insert_func: _symbol_insert_func(address, name)

def insert_metadata_block(insert_idx, block):
    global file_metadata_dirtyidx, file_metadata_addresses, file_metadata_line0s, file_metadata_blocks
    file_metadata_addresses.insert(insert_idx, block.address)
    file_metadata_line0s.insert(insert_idx, None)
    file_metadata_blocks.insert(insert_idx, block)
    # Update how much of the sorted line number index needs to be recalculated.
    if file_metadata_dirtyidx is not None and insert_idx < file_metadata_dirtyidx:
        file_metadata_dirtyidx = insert_idx

def lookup_metadata_by_address(lookup_key):
    global file_metadata_addresses, file_metadata_blocks
    lookup_index = bisect.bisect_right(file_metadata_addresses, lookup_key)
    return file_metadata_blocks[lookup_index-1], lookup_index-1

def recalculate_line_count_index():
    global file_metadata_dirtyidx, file_metadata_line0s, file_metadata_blocks
    if file_metadata_dirtyidx is not None:
        line_count_start = 0
        if file_metadata_dirtyidx > 0:
            line_count_start = file_metadata_line0s[file_metadata_dirtyidx-1] + file_metadata_blocks[file_metadata_dirtyidx-1].line_count
        for i in range(file_metadata_dirtyidx, len(file_metadata_line0s)):
            file_metadata_line0s[i] = line_count_start
            line_count_start += file_metadata_blocks[i].line_count
        file_metadata_dirtyidx = None

def lookup_metadata_by_line_count(lookup_key):
    global file_metadata_dirtyidx, file_metadata_line0s, file_metadata_blocks
    # If there's been a block insertion, update the cumulative line counts.
    recalculate_line_count_index()

    lookup_index = bisect.bisect_right(file_metadata_line0s, lookup_key)
    return file_metadata_blocks[lookup_index-1], lookup_index-1

def split_block(address):
    block, block_idx = lookup_metadata_by_address(address)
    if block.address == address:
        return None

    # How long the new block will be.
    excess_length = block.length - (address - block.address)

    # Truncate the preceding block the address is currently within.
    block.length -= excess_length

    # Create a new block for the address we are processing.
    new_block = SegmentBlock()
    new_block.flags = block.flags & BLOCK_SPLIT_BITMASK
    new_block.segment_id = block.segment_id
    new_block.address = block.address + block.length
    new_block.segment_offset = block.segment_offset + block.length
    new_block.length = excess_length
    insert_metadata_block(block_idx+1, new_block)

    if get_data_type(block.flags) == DATA_TYPE_CODE:
        num_bytes = 0
        for i, match in enumerate(block.line_data):
            if num_bytes == block.length:
                break
            num_bytes += match.num_bytes
        new_block.line_data = block.line_data[i:]
        block.line_data[i:] = []

    calculate_line_count(block)
    calculate_line_count(new_block)

    return new_block, block_idx+1

def UI_display_file(file_path):
    global file_metadata_dirtyidx, file_metadata_line0s, file_metadata_addresses, file_metadata_blocks
    global branch_addresses, symbols_by_address
    global file_info

    file_info = archlib.load_file(file_path)
    if file_info is None:
        return 0

    branch_addresses = set()
    symbols_by_address = {}

    # Two lists to help bisect do the searching, as it can't look into the blocks to get the sort value.
    file_metadata_blocks = []
    file_metadata_addresses = []
    file_metadata_line0s = []
    file_metadata_dirtyidx = 0

    entrypoint_segment_id, entrypoint_offset = file_info.get_entrypoint()
    entrypoint_address = file_info.get_segment_address(entrypoint_segment_id) + entrypoint_offset

    # Pass 1: Create a block for each of the segments.
    for segment_id in range(len(file_info.segments)):
        address = file_info.get_segment_address(segment_id)
        data_length = file_info.get_segment_data_length(segment_id)
        segment_length = file_info.get_segment_length(segment_id)

        block = SegmentBlock()
        if file_info.get_segment_type(segment_id) == archlib.SEGMENT_TYPE_BSS:
            block.flags |= BLOCK_FLAG_ALLOC
        set_data_type(block, DATA_TYPE_LONGWORD)
        block.segment_id = segment_id

        block.segment_offset = 0
        block.address = address
        block.length = data_length
        calculate_line_count(block)
        file_metadata_addresses.append(block.address)
        file_metadata_line0s.append(None)
        file_metadata_blocks.append(block)

        if segment_length > data_length:
            block = SegmentBlock()
            block.flags |= BLOCK_FLAG_ALLOC
            set_data_type(block, DATA_TYPE_LONGWORD)
            block.segment_id = segment_id
            block.address = address + data_length
            block.segment_offset = data_length
            block.length = segment_length - data_length
            calculate_line_count(block)
            file_metadata_addresses.append(block.address)
            file_metadata_line0s.append(None)
            file_metadata_blocks.append(block)

    # Pass 2: Stuff.

    # Incorporate known symbols.
    for segment_id in range(file_info.get_segment_count()):
        symbols = file_info.symbols_by_segment_id[segment_id]
        address = file_info.get_segment_address(segment_id)
        for symbol_offset, symbol_name, code_flag in symbols:
            insert_symbol(address + symbol_offset, symbol_name)
            # TODO: This causes errors when the code is actually in the bss, or non-file backed memory.
            #if code_flag:
            #    insert_branch_address(symbol_address, None)

    # Pass 3: Do another block splitting pass.
    for address in symbols_by_address.iterkeys():
        split_block(address)
    for address in file_info.relocated_addresses:
        split_block(address)

    # Pass 4: Do a disassembly pass.
    disassembly_offsets = [ entrypoint_address ]
    for address in branch_addresses:
        if address != entrypoint_address:
            disassembly_offsets.append(address)

    disassembly_checklist = {}
    while len(disassembly_offsets):
        address = disassembly_offsets.pop()
        #logger.debug("Processing address: %X", address)

        block, block_idx = lookup_metadata_by_address(address)
        if address - block.address > 0:
            # The middle of an existing block.
            if get_data_type(block.flags) == DATA_TYPE_CODE:
                # Code blocks are just split (if address is valid) and that's it.
                if address & 1 == 0:
                    split_block(address)
                continue
            # Split a block off at the processing point.
            new_block, block_idx = split_block(address)
            new_block.flags |= block.flags & BLOCK_FLAG_PROCESSED
            block = new_block

        if block.flags & BLOCK_FLAG_PROCESSED:
            #logger.debug("Address already processed: %X", address)
            continue

        block.flags |= BLOCK_FLAG_PROCESSED

        bytes_consumed = 0
        line_data = []
        found_terminating_instruction = False
        while bytes_consumed < block.length:
            data = file_info.get_segment_data(block.segment_id)
            data_offset_start = block.segment_offset + bytes_consumed
            match, data_offset_end = archm68k.disassemble_one_line(data, data_offset_start, address + bytes_consumed)
            bytes_matched = data_offset_end - data_offset_start
            if match is None or bytes_consumed + bytes_matched > block.length:
                logger.error("unable to disassemble at %X (started at %X)", address + bytes_consumed, address)
                break
            line_data.append(match)
            bytes_consumed += bytes_matched
            found_terminating_instruction = archm68k.is_final_instruction(match)
            if found_terminating_instruction:
                break

        if len(line_data) == 0:
            continue

        # Discard any unprocessed block.
        if bytes_consumed < block.length:
            trailing_block, trailing_block_idx = split_block(address + bytes_consumed)
            set_data_type(trailing_block, DATA_TYPE_LONGWORD)
            if not found_terminating_instruction:
                trailing_block.flags |= BLOCK_FLAG_PROCESSED

        set_data_type(block, DATA_TYPE_CODE)
        block.line_data = line_data
        calculate_line_count(block)
        disassembly_checklist[block.address] = None

        # Extract any addresses which are referred to, for later use.
        for match in line_data:
            for match_address, is_code in archm68k.get_match_addresses(match):
                if is_code and match_address not in disassembly_checklist:
                    disassembly_offsets.insert(0, match_address)
                insert_branch_address(match_address, address)

    for address in file_info.relocated_addresses:
        if address not in symbols_by_address:
            block, block_idx = lookup_metadata_by_address(address)
            if block.address != address:
                logger.error("Tried to label a relocated address without a block: %X", address)
                continue
            insert_symbol(address, "lb"+ char_by_data_type[get_data_type(block.flags)] + ("%06X" % address))

    if entrypoint_address not in symbols_by_address:
        insert_symbol(entrypoint_address, "ENTRYPOINT")

    recalculate_line_count_index()
    line_count = file_metadata_line0s[-1] + file_metadata_blocks[-1].line_count
    line_count += END_INSTRUCTION_LINES # blank line, then "end" instruction
    return line_count
    

if __name__ == "__main__":
    logger.setLevel(logging.DEBUG)
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG)
    logger.addHandler(ch)
