##############################################################################
# 73_MatterWS.pm
#
# FHEM client for the Open Home Foundation Matter Server, spoken over the
# WebSocket interface it already offers (matterjs-server, and the archived
# python-matter-server before it — the protocol is the same).
#
# The point of this module is how little it does.  The Matter protocol itself
# — commissioning, certificates, CASE/PASE, the whole SDK — lives in that
# server, which is a certified component maintained elsewhere.  What is left
# for FHEM is a socket that DevIo can already open (`ws:` has been in DevIo
# for years) and JSON that Perl decodes in one line.  Nothing here needs a
# CPAN module beyond JSON, which FHEM ships with.
#
#   define <name> MatterWS ws:<host>:<port>/ws
#   e.g.  define matter MatterWS ws:127.0.0.1:5580/ws
#
# Readings arrive as they change: the server pushes one message per changed
# attribute, and each carries exactly three things — the node, the attribute
# path endpoint/cluster/attribute, and the new value.
#
#   set <name> <node> on|off|toggle
#   set <name> <node> pct <0..100>
#
##############################################################################
package main;

use strict;
use warnings;
use JSON;
use DevIo;

# The handful of attribute paths worth a readable name.  Everything else is
# still reported, under its raw path — better a reading nobody named than a
# value silently dropped.
my %KNOWN = (
    "6/0"   => "onoff",        # OnOff cluster, OnOff attribute
    "8/0"   => "brightness",   # LevelControl, CurrentLevel  (0..254)
    "768/7" => "colorTempMireds",
    "768/8" => "colorMode",
    "769/0" => "localTemperature",
);

sub MatterWS_Initialize {
    my ($hash) = @_;
    $hash->{DefFn}      = \&MatterWS_Define;
    $hash->{UndefFn}    = \&MatterWS_Undef;
    $hash->{SetFn}      = \&MatterWS_Set;
    $hash->{ReadFn}     = \&MatterWS_Read;
    $hash->{ReadyFn}    = \&MatterWS_Ready;
    $hash->{AttrList}   = $readingFnAttributes;
    return;
}

sub MatterWS_Define {
    my ($hash, $def) = @_;
    my @a = split m{\s+}, $def;
    return "usage: define <name> MatterWS ws:<host>:<port>/ws" if (@a != 3);

    my $name = $a[0];
    DevIo_CloseDev($hash);
    $hash->{DeviceName} = $a[2];
    $hash->{MSGID}      = 0;
    $hash->{BUF}        = "";

    # Websocket needs the callback form of DevIo_OpenDev — DevIo refuses
    # otherwise, and the refusal is easy to miss because it is a return value,
    # not a log line.
    return DevIo_OpenDev($hash, 0, \&MatterWS_Init, \&MatterWS_Callback);
}

sub MatterWS_Undef {
    my ($hash) = @_;
    DevIo_CloseDev($hash);
    return;
}

sub MatterWS_Ready {
    my ($hash) = @_;
    return DevIo_OpenDev($hash, 1, \&MatterWS_Init, \&MatterWS_Callback)
        if (!DevIo_IsOpen($hash));
    return;
}

sub MatterWS_Callback {
    my ($hash, $err) = @_;
    my $name = $hash->{NAME};
    Log3 $name, 2, "$name: $err" if ($err);
    return;
}

# Called once the socket is up.  One line asks the server to push everything
# from now on; the reply carries the current state of every node, so there is
# no separate "read everything once" step.
sub MatterWS_Init {
    my ($hash) = @_;
    $hash->{BUF} = "";
    MatterWS_Send($hash, { command => "start_listening" });
    readingsSingleUpdate($hash, "state", "listening", 1);
    return;
}

sub MatterWS_Send {
    my ($hash, $msg) = @_;
    $msg->{message_id} = ++$hash->{MSGID} . "";
    my $json = encode_json($msg);
    Log3 $hash->{NAME}, 5, "$hash->{NAME} > $json";
    DevIo_SimpleWrite($hash, $json, 2);      # 2 = ASCII; DevIo frames it
    return;
}

sub MatterWS_Read {
    my ($hash) = @_;
    my $name = $hash->{NAME};
    my $buf  = DevIo_SimpleRead($hash);
    return if (!defined $buf);

    # DevIo hands over decoded websocket payloads.  A large reply can still
    # arrive in pieces, so collect until the JSON parses rather than assuming
    # one read is one message.
    $hash->{BUF} .= $buf;
    my $msg = eval { decode_json($hash->{BUF}) };
    if ($@) {
        # Not complete yet.  Give up on anything absurdly large rather than
        # growing this buffer without end.
        $hash->{BUF} = "" if (length($hash->{BUF}) > 4_000_000);
        return;
    }
    $hash->{BUF} = "";
    MatterWS_Dispatch($hash, $msg);
    return;
}

sub MatterWS_Dispatch {
    my ($hash, $msg) = @_;
    my $name = $hash->{NAME};

    # The greeting: no message_id, no event, just facts about the fabric.
    if (!$msg->{event} && !defined $msg->{message_id} && $msg->{sdk_version}) {
        readingsBeginUpdate($hash);
        readingsBulkUpdate($hash, "sdkVersion",    $msg->{sdk_version});
        readingsBulkUpdate($hash, "schemaVersion", $msg->{schema_version});
        readingsBulkUpdate($hash, "fabricId",      $msg->{fabric_id});
        readingsEndUpdate($hash, 1);
        return;
    }

    # The answer to a pairing request.  It takes a minute or two and can fail
    # for reasons worth reading, so it goes into a reading rather than the log.
    if (defined $hash->{PAIRID} && ($msg->{message_id} // "") eq $hash->{PAIRID}) {
        delete $hash->{PAIRID};
        if (defined $msg->{error_code}) {
            readingsSingleUpdate($hash, "pairing",
                                 "failed: " . ($msg->{details} // $msg->{error_code}), 1);
        } elsif (ref $msg->{result} eq "HASH" && defined $msg->{result}{node_id}) {
            readingsSingleUpdate($hash, "pairing",
                                 "node $msg->{result}{node_id} added", 1);
            MatterWS_Send($hash, { command => "start_listening" });
        } else {
            readingsSingleUpdate($hash, "pairing", "done", 1);
        }
        return;
    }

    # A change on a device: [node_id, "endpoint/cluster/attribute", value].
    if (($msg->{event} // "") eq "attribute_updated") {
        my ($node, $path, $value) = @{ $msg->{data} };
        MatterWS_Attribute($hash, $node, $path, $value);
        return;
    }

    # The reply to start_listening carries every node with all its attributes.
    if (ref $msg->{result} eq "ARRAY") {
        my $nodes = 0;
        for my $n (@{ $msg->{result} }) {
            next if (ref $n ne "HASH" || !defined $n->{node_id});
            $nodes++;
            my $attrs = $n->{attributes} // {};
            MatterWS_Attribute($hash, $n->{node_id}, $_, $attrs->{$_})
                for (keys %$attrs);
            readingsSingleUpdate($hash, "node$n->{node_id}_available",
                                 ($n->{available} ? "yes" : "no"), 1);
        }
        readingsSingleUpdate($hash, "nodes", $nodes, 1) if ($nodes);
        return;
    }

    Log3 $name, 5, "$name: unhandled message " . encode_json($msg);
    return;
}

sub MatterWS_Attribute {
    my ($hash, $node, $path, $value) = @_;
    my ($ep, $cluster, $attr) = split m{/}, $path;
    return if (!defined $attr);

    # JSON true/false arrive as objects; make them readings a user can match on.
    $value = ($value ? "on" : "off")
        if (ref $value eq "JSON::PP::Boolean" && "$cluster/$attr" eq "6/0");
    $value = ($value ? 1 : 0)  if (ref $value eq "JSON::PP::Boolean");
    $value = encode_json($value) if (ref $value);

    my $known = $KNOWN{"$cluster/$attr"};
    my $reading = $known ? "node${node}_ep${ep}_$known"
                         : "node${node}_${ep}_${cluster}_${attr}";
    readingsSingleUpdate($hash, $reading, $value, 1);
    return;
}

sub MatterWS_Set {
    my ($hash, @a) = @_;
    my $name = shift @a;
    my ($node, $cmd, $arg) = @a;
    # FHEMWEB asks with a single "?" to learn which widgets to draw, so the
    # list has to be reachable before anything counts arguments.  It was not,
    # and the pairing field never appeared.
    $node = "?" if (!defined $node);

    # Commissioning is not addressed to a node — there is no node yet.
    if ($node eq "pair") {
        my $code = $cmd // "";
        $code =~ s/[-\s]//g;
        return "set $name pair <pairing code>  (11 digits, dashes optional)"
            if ($code !~ m{^\d{11}$} && $code !~ m{^\d{21}$});
        $hash->{PAIRID} = $hash->{MSGID} + 1;
        MatterWS_Send($hash, {
            command => "commission_with_code",
            args    => { code => $code },
        });
        readingsSingleUpdate($hash, "pairing", "running", 1);
        return;
    }

    if ($node !~ m{^\d+$}) {
        # FHEMWEB builds its widgets from this list.  Only the pairing code
        # belongs here: the other commands take a node number, and which
        # numbers exist is not something this list can know.
        return "Unknown argument $node, choose one of pair:textField";
    }

    return "set $name needs <node> <command>" if (!defined $cmd);

    # Endpoint 1 is where a light lives on every device seen so far; an
    # attribute makes it changeable without touching the code when one turns
    # up where it is not.
    my $ep = AttrVal($name, "endpoint", 1);

    if ($cmd eq "on" || $cmd eq "off" || $cmd eq "toggle") {
        MatterWS_Send($hash, {
            command => "device_command",
            args    => {
                node_id      => $node + 0,
                endpoint_id  => $ep + 0,
                cluster_id   => 6,
                command_name => ucfirst($cmd),      # On / Off / Toggle
                payload      => {},
            },
        });
        return;
    }

    if ($cmd eq "pct") {
        return "set $name <node> pct <0..100>" if (!defined $arg || $arg !~ m{^\d+$});
        my $level = int($arg * 254 / 100 + 0.5);
        MatterWS_Send($hash, {
            command => "device_command",
            args    => {
                node_id      => $node + 0,
                endpoint_id  => $ep + 0,
                cluster_id   => 8,
                command_name => "MoveToLevelWithOnOff",
                payload      => { level => $level, transitionTime => 0,
                                  optionsMask => 0, optionsOverride => 0 },
            },
        });
        return;
    }

    return "Unknown argument $cmd, choose one of on off toggle pct";
    # (pair is handled above; it is not addressed to a node)
}

1;

=pod
=item device
=item summary    control Matter devices through the Open Home Foundation Matter Server
=begin html

<a id="MatterWS"></a>
<h3>MatterWS</h3>
<ul>
  Talks to the Open Home Foundation Matter Server over its WebSocket
  interface. The Matter protocol runs in that server; this module carries JSON
  over a socket and turns pushed attribute changes into readings.
  <br><br>

  <a id="MatterWS-define"></a>
  <b>Define</b>
  <ul>
    <code>define &lt;name&gt; MatterWS ws:&lt;host&gt;:&lt;port&gt;/ws</code><br><br>
    The server listens on port 5580 by default:<br>
    <code>define matter MatterWS ws:127.0.0.1:5580/ws</code>
  </ul><br>

  <a id="MatterWS-set"></a>
  <b>Set</b>
  <ul>
    <li><code>set &lt;name&gt; &lt;node&gt; on|off|toggle</code></li>
    <li><code>set &lt;name&gt; &lt;node&gt; pct &lt;0..100&gt;</code></li>
  </ul><br>

  <b>Readings</b>
  <ul>
    One per attribute the server reports, named after the node and the
    attribute path. Known ones get a readable name
    (<code>node8_ep1_onoff</code>), the rest keep their raw path
    (<code>node8_1_768_16394</code>) so nothing is silently dropped.
  </ul>
</ul>

=end html
=cut
