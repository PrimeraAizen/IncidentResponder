package obs

// NATS connection shared by publishers (order) and subscribers (notification).
// Connection loss and recovery are logged with upstream="nats" so the Log
// Analyst can see them; /healthz reports the connection status.

import (
	"context"
	"fmt"
	"time"

	"github.com/nats-io/nats.go"
)

// ConnectNATS connects (and keeps reconnecting forever) to the NATS server.
// The reconnect buffer is disabled: while disconnected, Publish returns an error
// instead of silently queueing messages in memory.
func (o *Obs) ConnectNATS(url string) (*nats.Conn, error) {
	nc, err := nats.Connect(url,
		nats.Name(o.Service),
		nats.RetryOnFailedConnect(true),
		nats.MaxReconnects(-1),
		nats.ReconnectWait(time.Second),
		nats.ReconnectBufSize(-1),
		nats.DisconnectErrHandler(func(_ *nats.Conn, err error) {
			msg := "connection closed"
			if err != nil {
				msg = err.Error()
			}
			o.Log.Error("lost connection to nats", "upstream", "nats", "error", msg)
		}),
		nats.ReconnectHandler(func(_ *nats.Conn) {
			o.Log.Info("reconnected to nats", "upstream", "nats")
		}),
	)
	if err != nil {
		return nil, err
	}
	o.AddHealthCheck("nats", func(context.Context) error {
		if s := nc.Status(); s != nats.CONNECTED {
			return fmt.Errorf("nats %s", s)
		}
		return nil
	})
	return nc, nil
}
