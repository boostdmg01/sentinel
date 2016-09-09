import pdb
from pprint import pprint
import sys, os
sys.path.append( os.path.join( os.path.dirname(__file__), '..' ) )
sys.path.append( os.path.join( os.path.dirname(__file__), '..', 'lib' ) )
import base58
import hashlib
import re
from decimal import Decimal
import simplejson
import binascii


def is_valid_dash_address( address, network = 'mainnet' ):
    # Only public key addresses are allowed
    # A valid address is a RIPEMD-160 hash which contains 20 bytes
    # Prior to base58 encoding 1 version byte is prepended and
    # 4 checksum bytes are appended so the total number of
    # base58 encoded bytes should be 25.  This means the number of characters
    # in the encoding should be about 34 ( 25 * log2( 256 ) / log2( 58 ) ).
    dash_version = 140 if network == 'testnet' else 76

    # Check length (This is important because the base58 library has problems
    # with long addresses (which are invalid anyway).
    if ( ( len( address ) < 26 ) or ( len( address ) > 35 ) ):
        return False

    address_version = None

    try:
        decoded = base58.b58decode_chk(address)
        address_version = ord(decoded[0])
    except:
        # rescue from exception, not a valid Dash address
        return False

    if ( address_version != dash_version ):
        return False

    return True


def hashit( data ):
    return int( hashlib.sha256(data).hexdigest(), 16 )

# returns the masternode VIN of the elected winner
def elect_mn(**kwargs):
    current_block_hash = kwargs['block_hash']
    mn_list = kwargs['mnlist']

    # filter only enabled MNs
    enabled = [mn for mn in mn_list if mn.status == 'ENABLED']
    block_hash_hash = hashit( current_block_hash )

    candidates = []
    for mn in enabled:
        mn_vin_hash = hashit( mn.vin )
        diff = mn_vin_hash - block_hash_hash
        absdiff = abs( diff )
        candidates.append({ 'vin': mn.vin, 'diff': absdiff })

    candidates.sort( key = lambda k: k['diff'] )

    try:
        winner = candidates[0]['vin']
    except:
        winner = None

    return winner


def parse_masternode_status_vin(status_vin_string):
    status_vin_string_regex = re.compile( 'CTxIn\(COutPoint\(([0-9a-zA-Z]+),\\s*(\d+)\),' )

    m = status_vin_string_regex.match( status_vin_string )
    txid = m.group(1)
    index = m.group(2)

    vin = txid + '-' + index
    if (txid == '0000000000000000000000000000000000000000000000000000000000000000'):
        vin = None

    return vin


# create superblock logic -- probably need a join table for proposal, superblock linkage
def create_superblock( dashd, proposals, event_block_height ):
    from models import Superblock, GovernanceObject, Proposal
    import dashlib

    # don't create an empty superblock
    if ( len(proposals) == 0 ):
        print "No proposals!"
        return None

    budget_allocated = Decimal(0)
    budget_max       = dashlib.get_superblock_budget_allocation(dashd, event_block_height)

    print "  IN create_superblock"
    print "    current height: %d" % dashd.rpc_command('getblockcount')
    print "event_block_height: %d" % event_block_height
    print "       budget_max : %d" % budget_max
    print " "

    # TODO: probably use a sub-table to link proposals for RI
    payments = []
    for proposal in proposals:
        # fmt_string = "name: %s , rank: %4d , amount: %s <= %s"
        fmt_string = "name: %s, rank: %4d, hash: %s, amount: %s <= %s"

        # skip proposals that are too expensive...
        if (budget_allocated + proposal.payment_amount) > budget_max:
            print fmt_string % (
                proposal.name,
                proposal.rank,
                proposal.governance_object.object_hash,
                proposal.payment_amount,
                "skipped (blows the budget)",
            )
            continue

        print fmt_string % (
            proposal.name,
            proposal.rank,
            proposal.governance_object.object_hash,
            proposal.payment_amount,
            "adding",
        )

        # else add proposal and keep track of total budget allocation
        budget_allocated += proposal.payment_amount

        # TODO: probably use a sub-table to link proposals for RI
        payment = { 'address': proposal.payment_address,
                    'amount': proposal.payment_amount }
        payments.append( payment )

    # don't create an empty superblock
    if not payments:
        print "No proposals made the cut!"
        return None

    # deterministic superblocks can't have random names
    sbname = "sb" + str(event_block_height)

    # TODO: actually link this to Proposals in the DB and don't
    # actually include this info. This will enforce RI in the DB schema
    # also.
    sb = Superblock(
        name = sbname,
        event_block_height = event_block_height,
        payment_addresses = '|'.join( [str( pd['address'] ) for pd in payments] ),
        payment_amounts   = '|'.join( [str( pd['amount' ] ) for pd in payments] ),
    )

    return sb

def current_block_hash(dashd):
    height = dashd.rpc_command('getblockcount')
    block_hash = dashd.rpc_command('getblockhash', height)
    return block_hash

def get_superblock_budget_allocation(dashd, height=None):
    if height is None:
        height = dashd.rpc_command('getblockcount')
    return Decimal( dashd.rpc_command('getsuperblockbudget', height) )

def next_superblock_max_budget(dashd):
    cycle = dashd.superblockcycle()
    current_block_height = dashd.rpc_command('getblockcount')

    last_superblock_height = ( current_block_height / cycle ) * cycle
    next_superblock_height = last_superblock_height + cycle

    last_allocation = get_superblock_budget_allocation( dashd, last_superblock_height )
    next_allocation = get_superblock_budget_allocation( dashd, next_superblock_height )

    # not really, but Tyler's algo:
    next_superblock_max_budget = min( last_allocation, next_allocation )
    # actual = ...
    # next_superblock_max_budget = next_allocation

    return next_superblock_max_budget


DASHD_GOVOBJ_TYPES = {
    'proposal': 1,
    'superblock': 2,
}

# shims 'til we can fix the dashd side
def SHIM_serialise_for_dashd(sentinel_hex):
    # unpack
    obj = deserialise(sentinel_hex)

    # shim for dashd
    govtype = obj[0]

    # add 'type' attribute
    obj[1]['type'] = DASHD_GOVOBJ_TYPES[govtype]

    # change 'name' attribute
    dashd_preferred_object_name = "%s_name" % govtype
    obj[1][dashd_preferred_object_name] = obj[1]['name']
    del obj[1]['name']

    # superblock => "trigger" in dashd
    if govtype == 'superblock':
        obj[0] = 'trigger'

    # dashd expects an array (even though there is only a 1:1 relationship between govobj->class)
    obj = [obj]

    # re-pack
    dashd_hex = serialise(obj)
    return dashd_hex

# shims 'til we can fix the dashd side
def SHIM_deserialise_from_dashd(dashd_hex):
    # unpack
    obj = deserialise(dashd_hex)

    # shim from dashd
    # only one element in the array...
    obj = obj[0]

    # extract the govobj type
    govtype = obj[0]

    # superblock => "trigger" in dashd
    if govtype == 'trigger':
        obj[0] = govtype = 'superblock'

    # fix name
    dashd_preferred_object_name = "%s_name" % govtype
    if dashd_preferred_object_name in obj[1]:
        obj[1]['name'] = obj[1][dashd_preferred_object_name]
        del obj[1][dashd_preferred_object_name]

    # remove redundant 'type' attribute
    if 'type' in obj[1]:
        del obj[1]['type']

    # re-pack
    sentinel_hex = serialise(obj)
    return sentinel_hex

# convenience
def deserialise(hexdata):
    json = binascii.unhexlify(hexdata)
    obj  = simplejson.loads(json, use_decimal=True)
    return obj

def serialise(dikt):
    json = simplejson.dumps(dikt, sort_keys=True, use_decimal=True)
    hexdata = binascii.hexlify(json)
    return hexdata
