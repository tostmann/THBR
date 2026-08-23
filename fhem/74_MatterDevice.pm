##############################################################################
# 74_MatterDevice.pm
#
# One FHEM device per Matter node.  MatterWS talks to the Matter server and
# hands over everything the nodes report; this module turns each node into a
# device you can switch, with the readings that belong to it.
#
# Someone who pairs a lamp expects a lamp — not thirty readings on the server
# device.  That is the whole reason this module exists.
#
# Devices appear by themselves: the first message from an unknown node makes
# FHEM's autocreate define one.
##############################################################################
package main;

use strict;
use warnings;
use JSON;

# What a set command has to send, per capability.  Endpoint comes from the
# device, not from here: a lamp lives on 1, a plug may not.
my %COLOUR = ("6/0" => 1, "8/0" => 1);

sub MatterDevice_Initialize {
    my ($hash) = @_;
    $hash->{Match}    = "^MatterDevice:";
    $hash->{ParseFn}  = \&MatterDevice_Parse;
    $hash->{DefFn}    = \&MatterDevice_Define;
    $hash->{UndefFn}  = \&MatterDevice_Undef;
    $hash->{SetFn}    = \&MatterDevice_Set;
    $hash->{AttrList} = "endpoint IODev $readingFnAttributes";
    return;
}

sub MatterDevice_Define {
    my ($hash, $def) = @_;
    my @a = split m{\s+}, $def;
    return "usage: define <name> MatterDevice <node-id>" if (@a != 3);
    my $node = $a[2];
    return "the node id is a number" if ($node !~ m{^\d+$});

    $hash->{NODE} = $node;
    $modules{MatterDevice}{defptr}{$node} = $hash;
    AssignIoPort($hash);
    readingsSingleUpdate($hash, "state", "present", 1) if (!$hash->{READINGS}{state});
    return;
}

sub MatterDevice_Undef {
    my ($hash, $name) = @_;
    delete $modules{MatterDevice}{defptr}{ $hash->{NODE} } if (defined $hash->{NODE});
    return;
}

# Everything a node reports arrives here, one attribute at a time.
sub MatterDevice_Parse {
    my ($iohash, $msg) = @_;
    my ($node, $reading, $value);
    eval {
        ($node, $reading, $value) = @{ decode_json(substr($msg, length("MatterDevice:"))) };
        1;
    } or return "";

    my $hash = $modules{MatterDevice}{defptr}{$node};
    if (!$hash) {
        # Nobody owns this node yet.  Saying so is what makes autocreate act.
        return "UNDEFINED Matter_$node MatterDevice $node";
    }

    readingsBeginUpdate($hash);
    readingsBulkUpdate($hash, $reading, $value);

    # The one reading a user actually looks at.  A lamp's endpoint is 1 on
    # every device seen so far; the attribute exists for the ones where it is
    # not, and is consulted rather than assumed.
    my $ep = AttrVal($hash->{NAME}, "endpoint", 1);
    readingsBulkUpdate($hash, "state", $value) if ($reading eq "ep${ep}_onoff");

    # The product name is worth having as the device's label rather than as
    # yet another reading nobody reads.
    if ($reading eq "0_40_3" && $value ne "" && !AttrVal($hash->{NAME}, "alias", "")) {
        CommandAttr(undef, "$hash->{NAME} alias $value");
    }
    readingsEndUpdate($hash, 1);
    return $hash->{NAME};
}

sub MatterDevice_Set {
    my ($hash, @a) = @_;
    my $name = shift @a;
    my ($cmd, $arg) = @a;
    $cmd = "?" if (!defined $cmd);

    my $usage = "Unknown argument $cmd, choose one of on:noArg off:noArg "
              . "toggle:noArg pct:slider,0,1,100";
    return $usage if ($cmd eq "?");

    my $io = $hash->{IODev};
    return "no Matter server device attached" if (!$io);
    my $ep = AttrVal($name, "endpoint", 1);

    if ($cmd eq "on" || $cmd eq "off" || $cmd eq "toggle") {
        IOWrite($hash, {
            command => "device_command",
            args    => {
                node_id      => $hash->{NODE} + 0,
                endpoint_id  => $ep + 0,
                cluster_id   => 6,
                command_name => ucfirst($cmd),
                payload      => {},
            },
        });
        return;
    }

    if ($cmd eq "pct") {
        return "set $name pct <0..100>" if (!defined $arg || $arg !~ m{^\d+$});
        IOWrite($hash, {
            command => "device_command",
            args    => {
                node_id      => $hash->{NODE} + 0,
                endpoint_id  => $ep + 0,
                cluster_id   => 8,
                command_name => "MoveToLevelWithOnOff",
                payload      => { level => int($arg * 254 / 100 + 0.5),
                                  transitionTime => 0,
                                  optionsMask => 0, optionsOverride => 0 },
            },
        });
        return;
    }

    return $usage;
}

1;

=pod
=item device
=item summary    a Matter node as a FHEM device, through MatterWS
=begin html

<a id="MatterDevice"></a>
<h3>MatterDevice</h3>
<ul>
  One device per Matter node. <a href="#MatterWS">MatterWS</a> talks to the
  Matter server; this module turns each node it reports into a device with its
  own readings and switches.
  <br><br>
  Devices are created by autocreate the first time a node reports anything, so
  pairing a lamp is enough to get one.
  <br><br>
  <b>Define</b>
  <ul><code>define &lt;name&gt; MatterDevice &lt;node-id&gt;</code></ul>
  <br>
  <b>Set</b>
  <ul>
    <li><code>on</code>, <code>off</code>, <code>toggle</code></li>
    <li><code>pct &lt;0..100&gt;</code> — brightness</li>
  </ul>
  <br>
  <b>Attributes</b>
  <ul>
    <li><code>endpoint</code> — which endpoint carries the switch, 1 by
        default; every device seen so far uses 1.</li>
  </ul>
</ul>
=end html
=cut
